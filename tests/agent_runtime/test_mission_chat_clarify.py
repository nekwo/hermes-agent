from agent_runtime.mission_chat_clarify import MAX_CHOICES, MissionChatClarifyCapture


def test_capture_records_first_question_and_choices():
    cap = MissionChatClarifyCapture()
    assert not cap.requested
    assert cap.request is None

    sentinel = cap.callback("Which dev — launcher or backend?", ["launcher", "backend"])

    assert cap.requested
    assert cap.request == {
        "question": "Which dev — launcher or backend?",
        "choices": ["launcher", "backend"],
    }
    # Sentinel tells the model to end its turn with the question, and echoes the
    # offered options so the reply prose can present them.
    assert "end your turn" in sentinel.lower()
    assert "launcher" in sentinel and "backend" in sentinel


def test_capture_open_ended_question_has_no_choices():
    cap = MissionChatClarifyCapture()
    cap.callback("What repo is this in?")
    assert cap.request == {"question": "What repo is this in?"}


def test_capture_trims_to_max_choices_and_drops_blanks():
    cap = MissionChatClarifyCapture()
    cap.callback("Pick one", ["a", "   ", "b", "c", "d", "e"])
    assert cap.request["choices"] == ["a", "b", "c", "d"]
    assert len(cap.request["choices"]) == MAX_CHOICES


def test_capture_first_call_wins():
    # The model is told to stop after asking; if it asks twice anyway, the
    # question we already reported to the caller stays authoritative.
    cap = MissionChatClarifyCapture()
    cap.callback("first?")
    cap.callback("second?", ["x"])
    assert cap.request == {"question": "first?"}


def test_capture_empty_question_records_nothing():
    cap = MissionChatClarifyCapture()
    out = cap.callback("   ")
    assert not cap.requested
    assert cap.request is None
    assert "non-empty" in out
