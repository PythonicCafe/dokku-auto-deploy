import pytest

from dokku_auto_deploy.telegram import Telegram, TelegramError, parse_chat


@pytest.mark.parametrize(
    "chat, expected",
    [
        pytest.param("-1003508368629_2909", ("-1003508368629", "2909"), id="group-topic"),
        pytest.param("-1003508368629", ("-1003508368629", None), id="group"),
    ],
)
def test_parse_chat(chat, expected):
    assert parse_chat(chat) == expected


class TestSendMessage:
    def test_topic_goes_in_message_thread_id(self, fake_api):
        fake_api.routes["POST /botTOKEN/sendMessage"] = (200, {"ok": True})
        Telegram("TOKEN", fake_api.url).send_message("-100_29", "<b>hi</b>")
        ((path, fields),) = fake_api.posts()
        assert path == "/botTOKEN/sendMessage"
        assert fields == {"chat_id": "-100", "message_thread_id": "29", "text": "<b>hi</b>", "parse_mode": "HTML"}

    def test_plain_group_has_no_thread(self, fake_api):
        fake_api.routes["POST /botTOKEN/sendMessage"] = (200, {"ok": True})
        Telegram("TOKEN", fake_api.url).send_message("-100", "hi")
        ((_, fields),) = fake_api.posts()
        assert "message_thread_id" not in fields

    def test_api_error_shows_description_but_never_the_token(self, fake_api):
        fake_api.routes["POST /botTOKEN/sendMessage"] = (400, {"ok": False, "description": "chat not found"})
        with pytest.raises(TelegramError) as exc:
            Telegram("TOKEN", fake_api.url).send_message("-100", "hi")
        assert "chat not found" in str(exc.value) and "TOKEN" not in str(exc.value)

    def test_unreachable_api_never_shows_the_token(self):
        with pytest.raises(TelegramError) as exc:
            Telegram("TOKEN", "http://127.0.0.1:9").send_message("-100", "hi")
        assert "TOKEN" not in str(exc.value)

    def test_repr_hides_the_token(self):
        assert "TOKEN" not in repr(Telegram("TOKEN"))
