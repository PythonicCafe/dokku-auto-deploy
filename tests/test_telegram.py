import pytest

from dokku_auto_deploy.telegram import Telegram, TelegramError, parse_chat


@pytest.mark.parametrize(
    "chat, expected",
    [
        pytest.param("-1003508368629_2909", ("-1003508368629", "2909"), id="group-topic"),
        pytest.param("-1003508368629", ("-1003508368629", None), id="group"),
        pytest.param("@deploy_logs", ("@deploy_logs", None), id="channel-username-with-underscore"),
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

    def test_slow_api_raises_telegram_error(self, fake_api, monkeypatch):
        monkeypatch.setattr("dokku_auto_deploy.telegram.HTTP_TIMEOUT", 0.2)
        fake_api.routes["POST /botTOKEN/sendMessage"] = (200, {"ok": True})
        fake_api.response_delay = 1
        with pytest.raises(TelegramError, match="did not answer in 0.2s") as exc:
            Telegram("TOKEN", fake_api.url).send_message("-100", "hi")
        assert "TOKEN" not in str(exc.value)

    def test_unreachable_api_never_shows_the_token(self):
        with pytest.raises(TelegramError) as exc:
            Telegram("TOKEN", "http://127.0.0.1:9").send_message("-100", "hi")
        assert "TOKEN" not in str(exc.value)

    def test_repr_hides_the_token(self):
        assert "TOKEN" not in repr(Telegram("TOKEN"))


def test_token_with_a_newline_never_shows_in_the_error(fake_api):
    with pytest.raises(TelegramError) as exc:
        Telegram("123:AB\nSECRET", fake_api.url).send_message("-100", "hi")
    assert "SECRET" not in str(exc.value)
    assert "InvalidURL" in str(exc.value)
