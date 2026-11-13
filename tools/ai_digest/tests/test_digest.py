import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DigestError, clean_html, due_slot, load_config, load_env, process_lock
from digest import active_groups, inspect_article, main, payload_size, prepare, send, show_status, skip_article
from providers import ModelError, Summarizer, TransportError, post_json, send_feishu
from storage import Source, State, connect_readonly


SUMMARY = "文章介绍了企业最新季度经营情况及其增长原因。\n- 营收为100亿元，同比增长20%。\n- 净利润达到10亿元，同比增长5%。\n- 数据对应2026年第三季度，后续计划仍以公告为准。"


class FakeModel:
    def __init__(self):
        self.calls = []

    def summarize(self, title, text):
        self.calls.append((title, text))
        return SUMMARY, {"input_tokens": 50, "output_tokens": 60}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.source_path = self.root / "source" / "we_mp_rss.db"
        self.source_path.parent.mkdir()
        self.db = sqlite3.connect(self.source_path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE message_tasks(id TEXT PRIMARY KEY,name TEXT,status INTEGER,mps_id TEXT);
            CREATE TABLE feeds(id TEXT PRIMARY KEY,mp_name TEXT);
            CREATE TABLE articles(id TEXT PRIMARY KEY,mp_id TEXT,title TEXT,url TEXT,
                publish_time INTEGER,status INTEGER,has_content INTEGER,content TEXT);
        """)
        self.db.executemany("INSERT INTO feeds VALUES (?, ?)", [(x, "公众号" + x) for x in "ABCDEFGH"])
        self.db.executemany("INSERT INTO message_tasks VALUES (?, ?, 1, ?)", [
            ("tech", "科技新闻更新", json.dumps([{"id": x} for x in "ABC"])),
            ("ai", "AI更新", json.dumps([{"id": x} for x in "GH"])),
            ("stock", "股市更新", json.dumps([{"id": x} for x in "DEF"])),
        ])
        self.db.commit()
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        raw = json.loads(example.read_text())
        raw.update(source_db=str(self.source_path), state_dir=str(self.root / "state"))
        for group, name in zip(raw["groups"], ("tech", "ai", "stock")):
            group["task_id"] = name
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(raw))
        self.cfg = load_config(self.config_path)
        State.initialize(self.cfg["state_dir"], self.source_path, self.at(9, 0))
        self.state = State(self.cfg["state_dir"])
        self.addCleanup(self.state.close)
        self.source = Source(self.source_path)
        self.groups = active_groups(self.cfg, self.source)
        self.env = {"FEISHU_WEBHOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/test-only"}
        self.model = FakeModel()
        self.messages = []
        self.output = io.StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def at(self, hour, minute, second=0):
        from zoneinfo import ZoneInfo
        return datetime(2026, 11, 13, hour, minute, second, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    def article(self, key, mp="A", ready=True, published=None, body="<p>正文：营收100亿元，同比增长20%。</p>"):
        self.db.execute("INSERT INTO articles VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                        (key, mp, "标题" + key, "https://example.com/article/" + key,
                         published or self.at(10, 1), int(ready), body if ready else None))
        self.db.commit()

    def run_prepare(self, minute=22, **kwargs):
        return prepare(self.cfg, self.source, self.state, self.groups, self.model, self.at(10, minute), **kwargs)

    def run_send(self, minute=30, hour=10, sender=None, **kwargs):
        return send(self.cfg, self.source, self.state, self.groups, self.env, self.at(hour, minute),
                    sender=sender or (lambda group, env, text, now: self.messages.append((group["task_id"], text))), **kwargs)

    def test_no_content_and_other_groups_never_call_model(self):
        self.article("waiting", ready=False)
        self.article("excluded", mp="Z")
        result = self.run_prepare()
        self.assertEqual(result["waiting"], 1)
        self.assertEqual(self.model.calls, [])

    def events(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines() if line.startswith('{')]

    def test_empty_article_displays_metadata_until_manually_skipped(self):
        self.article("image", body='<p><img src="https://example.com/picture.png"></p>')
        self.run_prepare()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.state.skipped(), {})
        show_status(self.cfg, self.source, self.state, self.groups)
        error = next(e for e in self.events() if e["event"] == "summary_error")
        self.assertEqual(error["title"], "标题image")
        self.assertEqual(error["mp_name"], "公众号A")
        self.assertEqual(error["url"], "https://example.com/article/image")
        self.assertIn("人工确认", error["hint"])
        inspect_article(self.source, self.state, "image")
        detail = self.events()[-1]
        self.assertEqual(detail["cleaned_chars"], 0)
        self.assertFalse(detail["skipped"])

    def test_manual_skip_persists_ignores_retry_and_suppresses_error_and_send(self):
        self.article("image", body='<img src="picture.png">')
        self.run_prepare()
        source_before = hashlib.sha256(self.source_path.read_bytes()).digest()
        skip_article(self.source, self.state, "image", "image_only", self.at(10, 24))
        original = self.state.summaries()["image"]
        self.assertEqual(self.state.retry("summary"), 0)
        self.assertEqual(self.state.summaries()["image"], original)
        with closing(State(self.cfg["state_dir"])) as state:
            self.assertEqual(state.skipped()["image"]["reason"], "image_only")
            result = prepare(self.cfg, self.source, state, self.groups, self.model, self.at(11, 22))
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["calls"], 0)
            self.output.seek(0)
            self.output.truncate()
            show_status(self.cfg, self.source, state, self.groups)
            self.assertFalse(any(e["event"] == "summary_error" for e in self.events()))
        self.run_send()
        self.assertEqual(self.messages, [])
        self.assertEqual(self.state.delivered_or_queued("tech"), set())
        self.assertEqual(source_before, hashlib.sha256(self.source_path.read_bytes()).digest())
        # Changed HTML does not override a deliberate human skip.
        self.db.execute("UPDATE articles SET content='<p>后来补充的文字</p>' WHERE id='image'")
        self.db.commit()
        self.run_prepare()
        self.assertEqual(self.model.calls, [])

    def test_unskip_restores_eligibility_without_calling_model(self):
        self.article("image", body='<img src="picture.png">')
        self.run_prepare()
        skip_article(self.source, self.state, "image", "image_only", self.at(10, 24))
        self.assertEqual(self.state.unskip("image", self.at(10, 26)), 1)
        self.assertEqual(self.state.unskip("image", self.at(10, 27)), 0)
        self.assertFalse(self.state.skipped())
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.state.summaries()["image"]["attempts"], 0)
        self.assertIsNotNone(self.state.db.execute("SELECT unskipped_at FROM skipped_articles").fetchone()[0])
        self.db.execute("UPDATE articles SET content='<p>已补齐可读正文</p>' WHERE id='image'")
        self.db.commit()
        self.run_prepare(28)
        self.assertEqual(len(self.model.calls), 1)

    def test_skip_rejects_unknown_waiting_and_text_articles(self):
        self.article("text")
        self.article("waiting", ready=False)
        self.article("already_summarized", body='<img src="picture.png">')
        self.state.success("already_summarized", "hash", SUMMARY, {}, self.at(10, 20))
        for article_id in ("missing", "text", "waiting", "already_summarized"):
            with self.assertRaises(DigestError):
                skip_article(self.source, self.state, article_id, "image_only", self.at(10, 24))
        self.assertFalse(self.state.skipped())

    def test_pending_summary_counts_failures_waiting_and_unique_totals(self):
        self.article("waiting", ready=False)
        self.article("new", mp="B")
        self.article("failed", mp="C")
        self.article("blocked", mp="D")
        self.article("image", body='<img src="picture.png">')
        self.article("done", mp="G")
        self.article("unselected", mp="Z")
        self.state.failure("failed", "hash", 3, "格式失败", self.at(10, 20), 120)
        self.state.failure("blocked", "hash", 1, "输出上限", self.at(10, 20), 120, {"blocked": True})
        self.state.success("done", "hash", SUMMARY, {}, self.at(10, 20))
        skip_article(self.source, self.state, "image", "image_only", self.at(10, 24))
        self.db.execute("UPDATE message_tasks SET mps_id=? WHERE id='ai'", (json.dumps([{"id": "A"}, {"id": "G"}]),))
        self.db.commit()
        self.groups = active_groups(self.cfg, self.source)
        show_status(self.cfg, self.source, self.state, self.groups)
        groups = {e["task_id"]: e for e in self.events() if e["event"] == "group_status"}
        self.assertEqual(groups["tech"]["pending_summary"], 3)
        self.assertEqual(groups["tech"]["waiting_content"], 1)
        self.assertEqual(groups["tech"]["skipped"], 1)
        self.assertEqual(groups["ai"]["pending_summary"], 1)
        self.assertEqual(groups["stock"]["pending_summary"], 1)
        totals = self.events()[-1]
        self.assertEqual(totals["pending_summary"], 4)  # A occurs in two groups, count once.
        self.assertEqual(totals["skipped"], 1)

    def test_old_state_readonly_then_additive_upgrade_preserves_records(self):
        self.article("done")
        self.run_prepare()
        self.run_send()
        with self.state.db:
            self.state.db.execute("DROP TABLE skipped_articles")
            self.state.db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        with closing(State(self.cfg["state_dir"], readonly=True)) as state:
            self.assertFalse(state.skipped())
            show_status(self.cfg, self.source, state, self.groups)
            self.assertEqual(state.meta("schema_version"), "1")
        with closing(State(self.cfg["state_dir"])) as state:
            self.assertEqual(state.meta("schema_version"), "2")
            self.assertEqual(state.summaries()["done"]["summary"], SUMMARY)
            self.assertEqual(state.delivered_or_queued("tech"), {"done"})
            self.assertEqual(state.meta("since"), str(self.at(9, 0)))

    def test_cli_skip_inspect_list_unskip_and_missing_source_snapshot(self):
        self.article("image", body='<img src="picture.png">')
        args = ["--config", str(self.config_path)]
        for command in (["inspect", "--article", "image"],
                        ["skip", "--article", "image", "--reason", "image_only"], ["skipped"]):
            self.assertEqual(main(args + command), 0, self.output.getvalue())
        self.db.execute("DELETE FROM articles WHERE id='image'")
        self.db.commit()
        self.assertEqual(main(args + ["inspect", "--article", "image"]), 0)
        detail = self.events()[-1]
        self.assertTrue(detail["source_missing"])
        self.assertTrue(detail["skipped"])
        self.assertEqual(detail["title"], "标题image")
        self.assertEqual(main(args + ["unskip", "--article", "image"]), 0)

    def test_skip_respects_prepare_lock(self):
        self.article("image", body='<img src="picture.png">')
        with process_lock(self.cfg["state_dir"], "prepare"):
            result = main(["--config", str(self.config_path), "skip", "--article", "image", "--reason", "image_only"])
            self.assertEqual(result, 0)
            self.assertEqual(self.events()[-1]["event"], "already_running")
            self.assertFalse(self.state.skipped())

    def test_schedules_grouping_and_restart_dedup(self):
        self.article("a")
        self.article("g", "G")
        self.article("d", "D")
        self.run_prepare()
        self.run_prepare()
        self.assertEqual(len(self.model.calls), 3)
        self.run_send(29)
        self.assertEqual(self.messages, [])
        self.run_send(30)
        self.assertEqual([g for g, text in self.messages], ["tech"])
        self.run_send(33)
        self.run_send(36)
        self.assertEqual([g for g, text in self.messages], ["tech", "ai", "stock"])
        with closing(State(self.cfg["state_dir"])) as restarted:
            send(self.cfg, self.source, restarted, self.groups, self.env, self.at(11, 30),
                 sender=lambda *args: self.fail("re-sent after restart"))
        self.assertNotIn("公众号G", self.messages[0][1])

    def test_multiple_articles_not_just_latest_and_late_content(self):
        self.article("older", ready=False, published=self.at(9, 30))
        self.article("new1")
        self.article("new2")
        self.run_prepare()
        self.run_send()
        self.assertIn("new1", self.messages[0][1])
        self.assertIn("new2", self.messages[0][1])
        self.db.execute("UPDATE articles SET content='<p>较早文章的完整正文，收入20亿元。</p>',has_content=1 WHERE id='older'")
        self.db.commit()
        self.run_prepare(42)
        self.run_send(42)
        self.assertEqual(len(self.messages), 1)
        self.run_send(30, hour=11)
        self.assertIn("older", self.messages[1][1])
        self.assertNotIn("new1", self.messages[1][1])

    def test_empty_slot_is_closed_and_later_arrival_waits(self):
        self.run_send()
        self.article("late")
        self.run_prepare(31)
        self.run_send(31)
        self.assertEqual(self.messages, [])
        self.run_send(30, hour=11)
        self.assertEqual(len(self.messages), 1)

    def test_send_failure_retries_cached_message_outside_schedule(self):
        self.article("a")
        self.run_prepare()
        def fail(*args):
            raise DigestError("模拟网络失败")
        self.run_send(sender=fail)
        self.run_prepare(40)
        self.run_send(40)
        self.run_send(41)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.messages), 1)

    def test_partial_split_retry_does_not_repeat_successful_parts(self):
        self.cfg["message_max_bytes"] = 2000
        for i in range(5):
            self.article(str(i))
        self.run_prepare()
        attempts = []
        def partial(group, env, text, now):
            attempts.append(text)
            if len(attempts) == 2:
                raise DigestError("模拟第二段失败")
        self.run_send(sender=partial)
        count = self.state.db.execute("SELECT count(*) FROM messages").fetchone()[0]
        self.assertGreater(count, 1)
        self.run_send(40, sender=partial)
        self.assertEqual(attempts.count(attempts[0]), 1)
        self.assertEqual(attempts.count(attempts[1]), 2)
        self.assertTrue(all(payload_size(text) < 2000 for text in attempts))

    def test_same_article_reuses_summary_across_groups(self):
        self.db.execute("UPDATE message_tasks SET mps_id=? WHERE id='ai'", (json.dumps([{"id": "A"}]),))
        self.db.commit()
        self.groups = active_groups(self.cfg, self.source)
        self.article("shared")
        self.run_prepare()
        self.run_send()
        self.run_send(33)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.messages), 2)

    def test_disabled_and_empty_groups_do_not_select_all(self):
        self.db.execute("UPDATE message_tasks SET mps_id='[]' WHERE id='tech'")
        self.db.execute("UPDATE message_tasks SET status=0 WHERE id='ai'")
        self.db.commit()
        groups, problems = self.source.groups(self.cfg["groups"])
        self.assertEqual([g["task_id"] for g in groups], ["stock"])
        self.assertEqual(len(problems), 1)

    def test_membership_change_cancels_stale_pending_message(self):
        self.article("a")
        self.run_prepare()
        self.run_send(sender=lambda *args: (_ for _ in ()).throw(DigestError("失败")))
        self.db.execute("UPDATE message_tasks SET mps_id=? WHERE id='tech'", (json.dumps([{"id": "B"}]),))
        self.db.commit()
        self.groups = active_groups(self.cfg, self.source)
        result = self.run_send(40)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self.messages, [])

    def test_source_readonly_and_no_source_changes(self):
        self.article("a")
        before = hashlib.sha256(self.source_path.read_bytes()).digest()
        with closing(connect_readonly(self.source_path)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM articles")
        self.run_prepare()
        self.run_send()
        self.assertEqual(before, hashlib.sha256(self.source_path.read_bytes()).digest())

    def test_reading_live_wal_sees_committed_content(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.article("wal", ready=False)
        self.db.execute("UPDATE articles SET content='<p>已补齐的正文</p>',has_content=1 WHERE id='wal'")
        self.assertIsNone(self.source.content("wal"))
        self.db.commit()
        self.assertEqual(self.source.content("wal"), "<p>已补齐的正文</p>")
        self.run_prepare()
        self.assertEqual(len(self.model.calls), 1)

    def test_dry_run_has_no_external_calls_or_state_mutations(self):
        self.article("a")
        self.run_prepare(dry_run=True)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.state.summaries(), {})
        self.run_prepare()
        self.run_send(dry_run=True, immediate=True)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM slots").fetchone()[0], 0)

    def test_model_failures_are_bounded_and_manual_retry_preserves_cache(self):
        self.article("bad")
        self.model.summarize = Mock(side_effect=DigestError("模型失败"))
        for minute in (22, 25, 30, 40):
            self.run_prepare(minute)
        self.assertEqual(self.model.summarize.call_count, 3)
        self.state.retry("summary")
        self.run_prepare(41)
        self.assertEqual(self.model.summarize.call_count, 4)

    def test_output_limit_is_persistently_blocked_until_explicit_retry(self):
        self.article("truncated")
        model = Summarizer(self.cfg["model"], {"MINIMAX_API_KEY": "test-key"})
        response = {"stop_reason": "max_tokens", "usage": {"input_tokens": 20, "output_tokens": 4096},
                    "content": [{"type": "thinking", "thinking": "private-thinking"}]}
        with patch("providers.post_json", return_value=response) as post:
            prepare(self.cfg, self.source, self.state, self.groups, model, self.at(10, 22))
            with closing(State(self.cfg["state_dir"])) as restarted:
                prepare(self.cfg, self.source, restarted, self.groups, model, self.at(11, 22))
            self.assertEqual(post.call_count, 1)
            record = self.state.summaries()["truncated"]
            diagnostics = json.loads(record["usage_json"])
            self.assertEqual(record["attempts"], 1)
            self.assertTrue(diagnostics["blocked"])
            self.assertEqual(diagnostics["usage"]["output_tokens"], 4096)
            self.assertEqual(diagnostics["stop_reason"], "max_tokens")
            self.assertFalse(diagnostics["has_text"])
            self.assertIn("elapsed_ms", diagnostics)
            self.assertNotIn("private-thinking", self.output.getvalue())
            self.assertNotIn("test-key", self.output.getvalue())
            self.state.retry("summary")
            post.return_value = {"stop_reason": "end_turn", "content": [{"type": "text", "text": SUMMARY}]}
            prepare(self.cfg, self.source, self.state, self.groups, model, self.at(11, 24))
            self.assertEqual(self.state.summaries()["truncated"]["summary"], SUMMARY)
            self.assertEqual(post.call_count, 2)

    def test_configuration_error_stops_batch_and_transient_error_does_not(self):
        self.article("a")
        self.article("b")
        model = Summarizer(self.cfg["model"], {"MINIMAX_API_KEY": "key"})
        with patch("providers.post_json", side_effect=TransportError("HTTP 状态 401", 401)) as post:
            result = prepare(self.cfg, self.source, self.state, self.groups, model, self.at(10, 22))
            self.assertEqual(result["failed"], 1)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(len(self.state.summaries()), 1)
        self.state.retry("summary")
        with patch("providers.post_json", side_effect=TransportError("HTTP 状态 429", 429)) as post:
            prepare(self.cfg, self.source, self.state, self.groups, model, self.at(10, 24))
            self.assertEqual(post.call_count, 2)
            prepare(self.cfg, self.source, self.state, self.groups, model, self.at(10, 25))
            self.assertEqual(post.call_count, 2)
            prepare(self.cfg, self.source, self.state, self.groups, model, self.at(10, 27))
            self.assertEqual(post.call_count, 4)

    def test_long_input_is_not_silently_truncated_or_sent(self):
        self.cfg["model"]["max_input_chars"] = 5
        self.article("long")
        self.run_prepare()
        self.assertEqual(self.model.calls, [])
        self.assertIn("未截断", self.state.summaries()["long"]["last_error"])

    def test_cli_dry_run_and_status_do_not_need_credentials(self):
        self.article("a")
        for command in (["prepare", "--dry-run"], ["send", "--now", "--dry-run"], ["status"]):
            self.assertEqual(main(["--config", str(self.config_path), *command]), 0, self.output.getvalue())

    def test_initialize_does_not_reset_baseline(self):
        self.assertFalse(State.initialize(self.cfg["state_dir"], self.source_path, self.at(11, 0)))
        self.assertEqual(float(self.state.meta("since")), self.at(9, 0))

    def test_send_retry_limit_and_manual_reset(self):
        self.article("a")
        self.run_prepare()
        self.cfg["send_max_attempts"] = 2
        sender = Mock(side_effect=DigestError("飞书故障"))
        for minute in (30, 40, 50):
            self.run_send(minute, sender=sender)
        self.assertEqual(sender.call_count, 2)
        self.state.retry("send")
        self.run_send(51)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(len(self.model.calls), 1)

    def test_destination_change_does_not_reuse_stale_pending_message(self):
        self.article("a")
        self.run_prepare()
        self.run_send(sender=lambda *args: (_ for _ in ()).throw(DigestError("失败")))
        self.env["FEISHU_WEBHOOK"] = "https://open.feishu.cn/open-apis/bot/v2/hook/new-target"
        self.assertEqual(self.run_send(40)["cancelled"], 1)
        self.run_send(30, hour=11)
        self.assertEqual(len(self.messages), 1)

    def test_cli_process_smoke_with_fake_executable(self):
        self.article("smoke")
        executable = self.root / "fake-claude"
        executable.write_text('#!' + sys.executable + '\nimport json,sys\n'
            'article=json.load(sys.stdin)\nassert "article" in article\n'
            'assert sys.argv[sys.argv.index("--tools")+1] == ""\n'
            'print(' + repr(json.dumps({"subtype": "success", "result": SUMMARY})) + ')\n')
        executable.chmod(0o700)
        raw = json.loads(self.config_path.read_text())
        raw["model"]["provider"] = "claude_cli"
        raw["model"]["claude_command"] = str(executable)
        self.config_path.write_text(json.dumps(raw))
        entry = str(Path(__file__).resolve().parents[1] / "digest.py")
        command = [sys.executable, entry, "--config", str(self.config_path)]
        for operation in (["prepare", "--limit", "1"], ["prepare", "--limit", "1"],
                          ["send", "--group", "tech", "--now", "--dry-run"]):
            result = subprocess.run(command + operation, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state.summaries()["smoke"]["attempts"], 1)
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_independent_locks(self):
        with process_lock(self.cfg["state_dir"], "prepare") as first:
            with process_lock(self.cfg["state_dir"], "prepare") as second:
                with process_lock(self.cfg["state_dir"], "send") as sender:
                    self.assertTrue(first)
                    self.assertFalse(second)
                    self.assertTrue(sender)


class PureTests(unittest.TestCase):
    def test_minimax_default_uses_subscription_key_and_ignores_thinking(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        model = Summarizer(config["model"], {"MINIMAX_API_KEY": "subscription-test-key"})
        with patch("providers.post_json", return_value={"stop_reason": "end_turn", "content": [
            {"type": "thinking", "thinking": "不要推送的思考内容"},
            {"type": "text", "text": SUMMARY}]} ) as post:
            summary, _ = model.summarize("标题", "正文")
            self.assertEqual(post.call_args.args[0], "https://api.minimax.cn/anthropic/v1/messages")
            self.assertEqual(post.call_args.args[2]["x-api-key"], "subscription-test-key")
            self.assertEqual(post.call_args.args[1]["model"], "MiniMax-M3.1-Flash")
            self.assertEqual(post.call_args.args[1]["output_config"], {"effort": "low"})
            self.assertEqual(summary, SUMMARY)

    def test_invalid_effort_rejected_before_any_model_call(self):
        path = Path(__file__).resolve().parents[1] / "config.example.json"
        raw = json.loads(path.read_text())
        raw["model"]["effort"] = "disabled"
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(raw))
            with self.assertRaises(DigestError):
                load_config(config)

    def test_api_format_failure_retains_usage_without_response_body(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        model = Summarizer(cfg["model"], {"MINIMAX_API_KEY": "key"})
        with patch("providers.post_json", return_value={"stop_reason": "end_turn", "usage": {"output_tokens": 99},
                "content": [{"type": "text", "text": "invalid-private-output"}]}):
            with self.assertRaises(ModelError) as raised:
                model.summarize("标题", "正文")
        self.assertEqual(raised.exception.category, "response_format")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.diagnostics["usage"]["output_tokens"], 99)
        self.assertNotIn("invalid-private-output", json.dumps(raised.exception.diagnostics))
        self.assertIn("elapsed_ms", raised.exception.diagnostics)

    def test_clean_html_preserves_numbers_and_removes_noise(self):
        html = '<style>bad</style><p>营收100亿元 &amp; 增长20%</p><video>播放器</video><br>关注<div>利润10亿元</div><script>bad</script><p>证券时报各平台所有原创内容说明</p><p>版权尾部</p>'
        self.assertEqual(clean_html(html), '营收100亿元 & 增长20%\n利润10亿元')

    def test_schedule_timezone_grace_and_hour_rollover(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 11, 13, 0, 1, tzinfo=tz).timestamp()
        self.assertEqual(due_slot(now, [59], tz, 300), "2026-11-12T23:59:00+08:00")
        self.assertIsNone(due_slot(now, [30], tz, 300))

    def test_env_file_is_not_executed_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('EXAMPLE_KEY="$(touch /must-not-exist)"\nEXAMPLE_OVERRIDE=file\n')
            with patch.dict(os.environ, {"EXAMPLE_OVERRIDE": "environment"}):
                env = load_env(path)
                self.assertEqual(env["EXAMPLE_KEY"], "$(touch /must-not-exist)")
                self.assertEqual(env["EXAMPLE_OVERRIDE"], "environment")

    def test_feishu_business_failure_and_signing(self):
        group = {"webhook_env": "HOOK", "sign_secret_env": "SIGN"}
        env = {"HOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/test-only", "SIGN": "test-secret"}
        with patch("providers.post_json", return_value={"code": 19024}) as post:
            with self.assertRaises(DigestError):
                send_feishu(group, env, "message", 1234)
            body = post.call_args.args[1]
            self.assertEqual(body["timestamp"], "1234")
            self.assertTrue(body["sign"])
        with patch("providers.post_json", return_value={"code": 0}):
            send_feishu(group, env, "message", 1234)

    def test_anthropic_sends_only_prompt_and_article(self):
        model = Summarizer({"provider": "anthropic", "name": "test-model", "api_url": "https://api.anthropic.com/v1/messages",
            "max_output_tokens": 1600, "max_input_chars": 60000, "timeout_seconds": 30},
            {"ANTHROPIC_API_KEY": "test-key", "FEISHU_WEBHOOK": "must-not-be-in-prompt"})
        with patch("providers.post_json", return_value={"stop_reason": "end_turn", "content": [{"type": "text", "text": SUMMARY}],
                                                      "usage": {"input_tokens": 123}}) as post:
            summary, usage = model.summarize("标题", "正文")
            body = post.call_args.args[1]
            self.assertEqual(summary, SUMMARY)
            self.assertEqual(usage["input_tokens"], 123)
            self.assertEqual(len(body["messages"]), 1)
            self.assertNotIn("tools", body)
            self.assertNotIn("must-not-be-in-prompt", json.dumps(body))
        with patch("providers.post_json", return_value={"stop_reason": "max_tokens"}):
            with self.assertRaises(DigestError):
                model.summarize("标题", "正文")

    def test_claude_uses_stdin_no_tools_and_no_webhook_env(self):
        model = Summarizer({"provider": "claude_cli", "max_input_chars": 60000,
                            "max_output_tokens": 1600, "timeout_seconds": 30},
                           {"PATH": "/usr/bin", "HOME": "/test", "FEISHU_WEBHOOK": "secret"})
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({"subtype": "success", "result": SUMMARY}), "")
        with patch("providers.shutil.which", return_value="/test/claude"), patch("providers.subprocess.Popen", return_value=process) as popen:
            model.summarize("标题", "全文正文")
            args = popen.call_args.args[0]
            self.assertEqual(args[args.index("--tools") + 1], "")
            self.assertIn("--safe-mode", args)
            self.assertNotIn("--continue", args)
            self.assertNotIn("FEISHU_WEBHOOK", popen.call_args.kwargs["env"])
            self.assertIn("全文正文", process.communicate.call_args.args[0])
            self.assertFalse(Path(popen.call_args.kwargs["cwd"]).exists())


if __name__ == "__main__":
    unittest.main()
