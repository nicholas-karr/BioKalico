import queue

from klippy.extras.image_display import ImageDisplay


def _bare_image_display(auth_token):
    obj = ImageDisplay.__new__(ImageDisplay)
    obj.auth_token = auth_token
    obj._cmd_queue = queue.Queue()
    return obj


def test_send_fire_and_forget_attaches_auth_token():
    # Commands queued without waiting for a reply (PROJECTOR_ON's retry and
    # the standby auto-off) must still carry auth_token.
    obj = _bare_image_display("secret-token")
    obj._send_fire_and_forget({"type": "PROJECTOR_ON"})

    cmd, result_q = obj._cmd_queue.get_nowait()
    assert cmd == {"type": "PROJECTOR_ON", "auth_token": "secret-token"}
    assert isinstance(result_q, queue.Queue)


def test_send_fire_and_forget_omits_token_when_unset():
    obj = _bare_image_display("")
    obj._send_fire_and_forget({"type": "PROJECTOR_OFF"})

    cmd, _ = obj._cmd_queue.get_nowait()
    assert cmd == {"type": "PROJECTOR_OFF"}
