"""AstrBot 协议桩测试；所有云端与 QQ 发送均为替身。"""

import asyncio
import json
import logging
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock


class Filters:
    class CustomFilter:
        def __init__(self, raise_error=True, **kwargs):
            self.raise_error = raise_error

    @staticmethod
    def custom_filter(*args, **kwargs):
        return lambda f: f

    @staticmethod
    def command(*args, **kwargs):
        return lambda f: f

    @staticmethod
    def llm_tool(*args, **kwargs):
        return lambda f: f

    @staticmethod
    def on_llm_request(*args, **kwargs):
        return lambda f: f


class Star:
    def __init__(self, context):
        self.context = context


class StarTools:
    root = ""

    @classmethod
    def get_data_dir(cls, _):
        return cls.root


class Record:
    def __init__(self, path):
        self.path = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)


def module(name, **values):
    m = types.ModuleType(name)
    m.__dict__.update(values)
    sys.modules[name] = m


try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    module("aiohttp")


module("astrbot")
module("astrbot.api", AstrBotConfig=dict, logger=logging.getLogger("whale_voice_test"))
module("astrbot.api.event", AstrMessageEvent=object, filter=Filters)
module(
    "astrbot.api.star",
    Star=Star,
    StarTools=StarTools,
    Context=object,
    register=lambda *a, **k: lambda f: f,
)
module("astrbot.api.message_components", Record=Record)
module(
    "astrbot.api.web",
    error_response=lambda msg, **kw: {
        "status": "error",
        "message": msg,
        "status_code": kw.get("status_code"),
        "data": kw.get("data"),
    },
    file_response=lambda *a, **kw: types.SimpleNamespace(path=a[0], **kw),
    request=types.SimpleNamespace(username="admin"),
)
from astrbot_plugin_whale_voice.core import VoiceError
from astrbot_plugin_whale_voice.main import (
    UnspacedSingFilter,
    UnspacedSpeechFilter,
    WhaleVoicePlugin,
)


class Plain:
    def __init__(self, text):
        self.text = text


class Event:
    def __init__(
        self,
        text="/语音 大家好",
        group="test-group",
        sender="test-user",
        mid="test-message",
        platform="qq-test",
        admin=False,
        cron=False,
        is_at_or_wake_command=True,
    ):
        self.text = text
        self.group = group
        self.sender = sender
        self.mid = mid
        self.platform = platform
        self.admin = admin
        self.cron = cron
        self.is_at_or_wake_command = is_at_or_wake_command
        self.sent = []
        self.message_obj = types.SimpleNamespace(message_id=mid)
        self.unified_msg_origin = platform + ":GroupMessage:" + group
        self.llm_enabled = True

    def get_messages(self):
        return [Plain(self.text)]

    def get_message_str(self):
        return self.text

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.sender

    def get_platform_id(self):
        return self.platform

    def get_platform_name(self):
        return "cron" if self.cron else "aiocqhttp"

    def get_extra(self, name):
        return {"id": "cron"} if self.cron and name == "cron_job" else None

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    def should_call_llm(self, value):
        self.llm_enabled = value

    async def send(self, result):
        self.sent.append(result)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        StarTools.root = self.temp.name
        source = {
            "provider": [
                {
                    "id": "embedding-test",
                    "type": "openai_embedding",
                    "enable": True,
                    "embedding_api_base": "https://api.siliconflow.cn/v1",
                    "embedding_api_key": "test-placeholder",
                }
            ]
        }
        self.context = types.SimpleNamespace(
            get_config=lambda: source,
            register_web_api=lambda *a: None,
            get_provider_by_id=lambda _: None,
        )
        self.config = {
            "enabled": True,
            "platform_id": "qq-test",
            "allowed_groups": ["test-group"],
            "speech_credential_provider_id": "embedding-test",
            "sing_provider_id": "",
            "voice": "claire",
        }
        self.plugin = WhaleVoicePlugin(self.context, self.config)
        self.event = Event()

        async def fake_audio(operation, text, raw, wav, singer, key):
            wav.write_bytes(b"RIFF" + b"x" * 30)
            return 2.0

        self.plugin._make_audio = AsyncMock(side_effect=fake_audio)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_initialize_and_status_never_generate(self):
        await self.plugin.initialize()
        status = await self.plugin.api_status()
        self.assertTrue(status["data"]["speech_ready"])
        self.assertFalse(status["data"]["singing_ready"])
        self.plugin._make_audio.assert_not_called()
        self.assertFalse(self.event.sent)

    async def test_voice_tool_sends_record_and_stores_no_text(self):
        text = "小蓝鲸提醒大家上课注意安全～"
        result = await self.plugin.whale_voice_reply(self.event, text)
        self.assertIn("QQ", result)
        self.assertEqual(len(self.event.sent), 1)
        self.assertIsInstance(self.event.sent[0][0], Record)
        records = list(self.plugin.records.glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "submitted_to_qq")
        self.assertNotIn(text, records[0].read_text(encoding="utf-8"))
        self.assertFalse(self.plugin.busy)

    async def test_duplicate_message_never_generates_twice(self):
        await self.plugin.whale_voice_reply(self.event, "你好")
        result = await self.plugin.whale_voice_reply(self.event, "你好")
        self.assertIn("已经处理", result)
        self.assertEqual(self.plugin._make_audio.call_count, 1)
        self.assertEqual(len(self.event.sent), 1)

    async def test_missing_song_does_not_call_paid_fallback(self):
        result = await self.plugin.whale_sing_short_song(self.event, "不存在的歌曲")
        self.assertIn("没有找到", result)
        self.plugin._make_audio.assert_not_called()
        self.assertFalse(self.plugin.gate.state["requests"])

    async def test_singing_configured_uses_separate_mode(self):
        cls = type("ProviderMiMoTTSAPI", (), {})
        singer = cls()
        singer.chosen_api_key = "test-placeholder"
        singer._build_style_prefix = lambda: "<style>唱歌</style>"
        self.plugin.config["sing_provider_id"] = "singer-test"
        self.context.get_provider_by_id = lambda _: singer
        await self.plugin.whale_create_short_song(self.event, "小鲸鱼游呀游")
        args = self.plugin._make_audio.call_args.args
        self.assertEqual(args[0], "sing")
        self.assertIs(args[4], singer)
        self.assertEqual(args[5], "")

    async def test_unauthorized_group_no_generation(self):
        self.event.group = "other-group"
        result = await self.plugin.whale_voice_reply(self.event, "你好")
        self.assertIn("暂时还没开放", result)
        self.plugin._make_audio.assert_not_called()

    async def test_private_only_admin(self):
        self.event.group = ""
        self.assertFalse(self.plugin._allowed(self.event))
        self.event.admin = True
        self.assertTrue(self.plugin._allowed(self.event))

    async def test_cron_cannot_trigger_paid_audio(self):
        self.event.cron = True
        await self.plugin.whale_voice_reply(self.event, "这是一份日报")
        self.plugin._make_audio.assert_not_called()
        self.assertFalse(self.event.sent)

    async def test_wrong_platform(self):
        self.event.platform = "other-platform"
        self.assertFalse(self.plugin._allowed(self.event))

    async def test_busy_no_reservation(self):
        self.plugin.busy = True
        with self.assertRaises(VoiceError):
            await self.plugin._perform(self.event, "你好", "speech")
        self.assertFalse(self.plugin.gate.state["requests"])

    async def test_failure_no_retry(self):
        self.plugin._make_audio = AsyncMock(side_effect=TimeoutError())
        result = await self.plugin.whale_voice_reply(self.event, "你好")
        self.assertIn("没有自动重试", result)
        result2 = await self.plugin.whale_voice_reply(self.event, "再来")
        self.assertIn("已经处理", result2)
        self.assertEqual(self.plugin._make_audio.call_count, 1)

    async def test_qq_failure_no_generation_retry(self):
        self.event.send = AsyncMock(side_effect=RuntimeError("fake refusal"))
        result = await self.plugin.whale_voice_reply(self.event, "你好")
        self.assertIn("没有自动重试", result)
        self.assertEqual(self.plugin._make_audio.call_count, 1)
        record = json.loads(next(self.plugin.records.glob("*.json")).read_text())
        self.assertEqual(record["status"], "failed_or_unknown")

    async def test_missing_message_identifier(self):
        self.event.message_obj.message_id = ""
        await self.plugin.whale_voice_reply(self.event, "你好")
        self.plugin._make_audio.assert_not_called()

    async def test_read_command_no_extra_llm(self):
        results = [x async for x in self.plugin.read_text(self.event)]
        self.assertFalse(results)
        self.assertFalse(self.event.llm_enabled)
        self.assertEqual(self.plugin._make_audio.call_args.args[1], "大家好")

    async def test_command_without_text_not_speaks_command_itself(self):
        self.event.text = "/语音"
        results = [x async for x in self.plugin.read_text(self.event)]
        self.assertTrue(results)
        self.plugin._make_audio.assert_not_called()

    async def test_voice_hint_only_requested(self):
        req = types.SimpleNamespace(system_prompt="原人格")
        self.event.text = "毕业学分有多少？"
        await self.plugin.voice_hint(self.event, req)
        self.assertEqual(req.system_prompt, "原人格")
        self.event.text = "用语音回答毕业要求"
        await self.plugin.voice_hint(self.event, req)
        self.assertIn("whale_voice_reply", req.system_prompt)

        # 自然语言放歌触发提示词注入与曲库感知
        req2 = types.SimpleNamespace(system_prompt="原人格")
        self.event.text = "放一下人是猫完整版听听"
        await self.plugin.voice_hint(self.event, req2)
        self.assertIn("whale_sing_short_song", req2.system_prompt)
        self.assertIn("人是猫", req2.system_prompt)

    async def test_help_honest_about_singing(self):
        results = [x async for x in self.plugin.help_voice(self.event)]
        self.assertIn("尚未配置", results[0])
        self.plugin._make_audio.assert_not_called()

    async def test_sing_command_empty_shows_songlist(self):
        self.event.text = "/唱歌"
        results = [x async for x in self.plugin.sing_text(self.event)]
        self.assertTrue(results)
        self.assertIn("想听小蓝鲸唱什么歌", results[0])
        self.plugin._make_audio.assert_not_called()

    async def test_sing_command_alias_play(self):
        self.event.text = "/播放 人是猫完整版"
        self.plugin._perform = AsyncMock()
        [x async for x in self.plugin.sing_text(self.event)]
        self.plugin._perform.assert_called_once_with(self.event, "人是猫完整版", "sing")

    async def test_sing_unspaced_command(self):
        self.event.text = "/播放人是猫完整版"
        self.plugin._perform = AsyncMock()
        [x async for x in self.plugin.sing_unspaced(self.event)]
        self.plugin._perform.assert_called_once_with(self.event, "人是猫完整版", "sing")

    async def test_read_unspaced_command(self):
        self.event.text = "/语音大家注意安全"
        self.plugin._perform = AsyncMock()
        [x async for x in self.plugin.read_unspaced(self.event)]
        self.plugin._perform.assert_called_once_with(self.event, "大家注意安全", "speech")

    def test_unspaced_filters(self):
        sing_filter = UnspacedSingFilter()
        speech_filter = UnspacedSpeechFilter()

        ev_play = Event(text="/播放人是猫完整版", is_at_or_wake_command=True)
        self.assertTrue(sing_filter.filter(ev_play, {}))
        self.assertFalse(speech_filter.filter(ev_play, {}))

        ev_sing = Event(text="/唱歌人是猫", is_at_or_wake_command=True)
        self.assertTrue(sing_filter.filter(ev_sing, {}))

        ev_space = Event(text="/播放 人是猫完整版", is_at_or_wake_command=True)
        self.assertFalse(sing_filter.filter(ev_space, {}))

        ev_chat = Event(text="今天在KTV唱歌很开心", is_at_or_wake_command=False)
        self.assertFalse(sing_filter.filter(ev_chat, {}))
        self.assertFalse(speech_filter.filter(ev_chat, {}))

    async def test_list_songs_command(self):
        results = [x async for x in self.plugin.list_songs(self.event)]
        self.assertTrue(results)
        self.assertIn("预置", results[0])

    async def test_unload_cancels_generation_and_no_send(self):
        started = asyncio.Event()

        async def waiting(*args):
            started.set()
            await asyncio.Event().wait()

        self.plugin._make_audio = AsyncMock(side_effect=waiting)
        task = asyncio.create_task(self.plugin._perform(self.event, "你好", "speech"))
        await started.wait()
        await self.plugin.terminate()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.event.sent)
        self.assertFalse(self.plugin.busy)


if __name__ == "__main__":
    unittest.main()
