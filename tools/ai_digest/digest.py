#!/usr/bin/env python3
"""Standalone WeRSS grouped digests. Does not import or modify the application."""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from common import DigestError, clean_html, due_slot, load_config, load_env, process_lock
from providers import Summarizer, send_feishu, target_hash, webhook_url
from storage import Source, State


def report(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def active_groups(cfg, source, selected=None):
    groups, problems = source.groups(cfg["groups"])
    for task_id, error in problems:
        report("group_error", task_id=task_id, error=error)
    if selected:
        groups = [group for group in groups if group["task_id"] == selected]
        if not groups:
            raise DigestError("选定分组不存在、未配置或未启用")
    return groups


def backoff(cfg, attempts):
    return min(3600, cfg["retry_seconds"] * 2 ** min(attempts - 1, 10))


def prepare(cfg, source, state, groups, summarizer, now, limit=None, dry_run=False):
    cache = state.summaries()
    mp_ids = set().union(*(group["mp_ids"] for group in groups))
    articles = source.articles(mp_ids, float(state.meta("since")))
    counts = dict(waiting=0, cached=0, calls=0, summarized=0, failed=0, deferred=0, candidates=0)
    limit = limit or cfg["prepare_limit"]
    for article in articles:
        old = cache.get(article["id"], {})
        if old.get("summary"):
            counts["cached"] += 1
            continue
        if not article["ready"]:
            counts["waiting"] += 1
            continue
        text = clean_html(source.content(article["id"]))
        digest = hashlib.sha256((article["title"] + "\n" + text).encode()).hexdigest()
        previous_attempts = old.get("attempts", 0) if old.get("input_hash") == digest else 0
        if old.get("input_hash") == digest and (
            previous_attempts >= cfg["summary_max_attempts"] or old.get("next_retry", 0) > now
        ):
            counts["deferred"] += 1
            continue
        if counts["calls"] >= limit:
            counts["deferred"] += 1
            continue
        counts["candidates"] += 1
        if dry_run:
            report("would_summarize", article_id=article["id"], title=article["title"], chars=len(text))
            # Count candidates against the preview limit without actually calling anything.
            if counts["candidates"] >= limit:
                break
            continue
        try:
            if not text:
                raise DigestError("清洗后正文为空")
            if len(text) + len(article["title"]) > cfg["model"]["max_input_chars"]:
                raise DigestError("正文超过 max_input_chars；未截断、未调用模型")
            counts["calls"] += 1
            summary, usage = summarizer.summarize(article["title"], text)
            state.success(article["id"], digest, summary, usage, now)
            counts["summarized"] += 1
            report("summary_saved", article_id=article["id"], title=article["title"], usage=usage)
        except DigestError as error:
            attempts = previous_attempts + 1
            state.failure(article["id"], digest, attempts, str(error), now, backoff(cfg, attempts))
            counts["failed"] += 1
            report("summary_failed", article_id=article["id"], error=str(error), attempts=attempts)
    report("prepare_finished", dry_run=dry_run, **counts)
    return counts


def payload_size(text):
    return len(json.dumps({"msg_type": "text", "content": {"text": text}},
                          ensure_ascii=False).encode("utf-8"))


def build_parts(group, articles, cache, cfg, now):
    label = datetime.fromtimestamp(now, cfg["timezone_obj"]).strftime("%Y-%m-%d %H:%M")
    header = f"【{group['name']}】{label}\n本轮新增 {len(articles)} 篇"
    parts, text, ids = [], header, []
    # Leave room for signing fields and any future envelope additions.
    budget = cfg["message_max_bytes"] - 512
    for article in articles:
        published = datetime.fromtimestamp(article["publish_time"], cfg["timezone_obj"]).strftime("%Y-%m-%d %H:%M")
        block = (f"\n\n{article['mp_name']}｜{article['title']}\n"
                 f"{cache[article['id']]['summary']}\n发布时间：{published}\n原文：{article['url'] or ''}")
        if payload_size(header + block) > budget:
            raise DigestError("单篇摘要超过飞书消息预算；请检查标题、摘要或 message_max_bytes")
        if ids and payload_size(text + block) > budget:
            parts.append((text, ids))
            text, ids = header, []
        text += block
        ids.append(article["id"])
    if ids:
        parts.append((text, ids))
    return parts


def send(cfg, source, state, groups, env, now, sender=send_feishu, dry_run=False, immediate=False):
    cache = state.summaries()
    by_id = {group["task_id"]: group for group in groups}
    live_ids = {}
    counts = dict(queued=0, sent=0, failed=0, cancelled=0)
    for group in groups:
        group_id = group["task_id"]
        articles = source.articles(group["mp_ids"], float(state.meta("since")))
        live_ids[group_id] = {article["id"] for article in articles}
        slot = ("manual:" + datetime.fromtimestamp(now, cfg["timezone_obj"]).isoformat()
                if immediate else due_slot(now, group["push_minutes"], cfg["timezone_obj"],
                                           cfg["schedule_grace_seconds"]))
        if not slot or state.has_slot(group_id, slot):
            continue
        used = state.delivered_or_queued(group_id)
        eligible = [article for article in articles if article["id"] not in used
                    and article["ready"] and cache.get(article["id"], {}).get("summary")]
        eligible = eligible[:cfg["max_articles_per_push"]]
        try:
            parts = build_parts(group, eligible, cache, cfg, now)
            if dry_run:
                for text, ids in parts:
                    report("would_send", task_id=group_id, article_ids=ids, text=text)
                report("slot_preview", task_id=group_id, slot=slot, articles=len(eligible))
                continue
            # No eligible articles still closes this slot; later arrivals wait for the next slot.
            fingerprint = target_hash(group, env) if parts else ""
            state.enqueue(group_id, slot, parts, fingerprint, now)
            counts["queued"] += len(parts)
            report("slot_created", task_id=group_id, slot=slot, articles=len(eligible), parts=len(parts))
        except DigestError as error:
            counts["failed"] += 1
            report("slot_failed", task_id=group_id, error=str(error))
    for message in state.pending(now, cfg["send_max_attempts"]):
        group = by_id.get(message["group_id"])
        if not group:
            # Disabled/removed groups stop sending immediately, including retries.
            continue
        if dry_run:
            report("pending_message", message_id=message["id"], task_id=message["group_id"],
                   article_ids=json.loads(message["article_ids"]))
            continue
        try:
            if (not set(json.loads(message["article_ids"])).issubset(live_ids[message["group_id"]])
                    or message["target_hash"] != target_hash(group, env)):
                # Rebuild on the next slot after membership/target changes; never send stale content.
                state.cancel(message["id"])
                counts["cancelled"] += 1
                report("message_cancelled", message_id=message["id"], reason="分组成员或目标变更")
                continue
            sender(group, env, message["text"], now)
        except DigestError as error:
            state.send_failed(message["id"], str(error), now, backoff(cfg, message["attempts"] + 1))
            counts["failed"] += 1
            report("send_failed", message_id=message["id"], error=str(error))
        else:
            # Keep this outside the HTTP exception handler: a local DB failure must not be
            # mistaken for an HTTP failure. Webhook delivery cannot be strictly exactly-once.
            state.sent(message["id"], now)
            counts["sent"] += 1
            report("message_sent", message_id=message["id"], task_id=message["group_id"])
    report("send_finished", dry_run=dry_run, **counts)
    return counts


def show_status(cfg, source, state, groups):
    cache = state.summaries()
    report("baseline", since=datetime.fromtimestamp(float(state.meta("since")),
                                                   cfg["timezone_obj"]).isoformat())
    for group in groups:
        articles = source.articles(group["mp_ids"], float(state.meta("since")))
        used = state.delivered_or_queued(group["task_id"])
        report("group_status", task_id=group["task_id"], name=group["name"],
               push_minutes=group["push_minutes"], articles=len(articles),
               waiting_content=sum(not article["ready"] for article in articles),
               ready_to_send=sum(bool(cache.get(a["id"], {}).get("summary"))
                                 and a["ready"] and a["id"] not in used for a in articles))
    for row in state.db.execute("""SELECT article_id, attempts, last_error FROM summaries
        WHERE summary IS NULL AND last_error IS NOT NULL"""):
        report("summary_error", **dict(row))
    for row in state.db.execute("""SELECT id, group_id, attempts, next_retry, last_error
        FROM messages WHERE status='pending'"""):
        report("pending_message", **dict(row))
    report("totals", summaries=sum(bool(row["summary"]) for row in cache.values()),
           sent_messages=state.db.execute("SELECT count(*) FROM messages WHERE status='sent'").fetchone()[0])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-tasks", help="只读列出任务 ID、名称和公众号")
    commands.add_parser("check", help="离线检查配置、源库与模型调用环境，不调用模型或发送消息")
    initialize = commands.add_parser("init", help="初始化独立状态库，不处理文章")
    initialize.add_argument("--since", help="文章发布时间下限，ISO 时间；默认当前时刻")
    commands.add_parser("status", help="只读查看进度与失败记录")
    for name in ("prepare", "send"):
        command = commands.add_parser(name)
        command.add_argument("--dry-run", action="store_true", help="只预览，不调用模型、不推送、不写状态")
        command.add_argument("--group", help="只处理此消息任务 ID")
        if name == "prepare":
            command.add_argument("--limit", type=int, help="本轮最多调用模型的篇数")
        else:
            command.add_argument("--now", action="store_true", help="立即生成本轮消息，仍按文章去重")
    retry = commands.add_parser("retry", help="解除失败重试上限；下次定时任务执行，不立即调用外部接口")
    retry.add_argument("kind", choices=("summary", "send"))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        cfg = load_config(args.config)
        env = load_env(cfg["env_file"])
        source = Source(cfg["source_db"])
        if args.command == "list-tasks":
            for task in source.tasks():
                report("task", **task)
            return 0
        groups = active_groups(cfg, source, getattr(args, "group", None))
        now = time.time()
        if args.command == "check":
            _groups, problems = source.groups(cfg["groups"])
            if problems:
                raise DigestError("存在无效分组，请修正上方 group_error 后再启用定时器")
            if not groups:
                raise DigestError("没有有效且启用的分组")
            for group in groups:
                webhook_url(group, env)
            Summarizer(cfg["model"], env).check()
            source.articles(set().union(*(g["mp_ids"] for g in groups)), now)
            report("check_ok", groups=len(groups), provider=cfg["model"]["provider"])
            return 0
        if args.command == "init":
            since = datetime.fromisoformat(args.since) if args.since else datetime.fromtimestamp(now, cfg["timezone_obj"])
            if since.tzinfo is None:
                since = since.replace(tzinfo=cfg["timezone_obj"])
            cfg["state_dir"].mkdir(parents=True, exist_ok=True, mode=0o700)
            with process_lock(cfg["state_dir"], "init") as acquired:
                if not acquired:
                    raise DigestError("另一个初始化进程正在运行")
                created = State.initialize(cfg["state_dir"], cfg["source_db"], since.timestamp())
            report("state_initialized" if created else "state_exists", note="已有状态库不会重设起始时间或去重记录")
            return 0
        readonly = args.command == "status" or getattr(args, "dry_run", False)
        if not (cfg["state_dir"] / "state.sqlite3").exists():
            raise DigestError("请先运行 init 初始化状态库")
        with closing(State(cfg["state_dir"], readonly=readonly)) as state:
            state.verify_source(cfg["source_db"])
            if args.command == "status":
                show_status(cfg, source, state, groups)
                return 0
            if args.command == "prepare" and args.limit is not None and args.limit <= 0:
                raise DigestError("--limit 必须为正整数")
            if readonly:
                if args.command == "prepare":
                    prepare(cfg, source, state, groups, None, now, args.limit, True)
                else:
                    send(cfg, source, state, groups, env, now, dry_run=True, immediate=args.now)
                return 0
            lock = args.command if args.command != "retry" else ("prepare" if args.kind == "summary" else "send")
            with process_lock(cfg["state_dir"], lock) as acquired:
                if not acquired:
                    report("already_running", command=args.command)
                    return 0
                if args.command == "prepare":
                    counts = prepare(cfg, source, state, groups, Summarizer(cfg["model"], env), now, args.limit)
                elif args.command == "send":
                    counts = send(cfg, source, state, groups, env, now, immediate=args.now)
                else:
                    report("retry_reset", kind=args.kind, records=state.retry(args.kind))
                    return 0
                return 1 if counts["failed"] else 0
    except DigestError as error:
        report("error", error=str(error))
    except sqlite3.Error:
        report("error", error="SQLite 访问失败：检查源库/状态库路径、目录及 WAL/SHM 权限、锁和表结构；不要复制运行中的数据库文件")
    except (OSError, ValueError, KeyError, TypeError):
        # Do not echo invalid config values, subprocess arguments, or raw network errors.
        report("error", error="文件、配置或响应格式错误；请检查配置字段和文件权限")
    except KeyboardInterrupt:
        report("interrupted")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
