from agent_bridge.platforms.slack.adapter import SlackAdapter


def test_format_single_question():
    questions = [{"question": "Which database should I use?"}]
    result = SlackAdapter._format_questions_for_slack(questions)
    assert "Claude needs your input" in result
    assert "Which database should I use?" in result
    assert "Reply in this thread to answer." in result
    # Single question should NOT have numbering
    assert "*1.*" not in result


def test_format_question_with_options():
    questions = [
        {
            "question": "Which theme?",
            "options": [
                {"value": "dark", "label": "Dark theme", "description": "Dark background"},
                {"value": "light", "label": "Light theme"},
            ],
        }
    ]
    result = SlackAdapter._format_questions_for_slack(questions)
    assert "`Dark theme` — Dark background" in result
    assert "`Light theme`" in result


def test_format_multi_select():
    questions = [
        {
            "question": "Which features?",
            "options": [{"label": "Auth"}, {"label": "Logging"}],
            "multiSelect": True,
        }
    ]
    result = SlackAdapter._format_questions_for_slack(questions)
    assert "You can select multiple" in result


def test_format_multiple_questions():
    questions = [
        {"question": "First question?"},
        {"question": "Second question?"},
    ]
    result = SlackAdapter._format_questions_for_slack(questions)
    assert "*1.* First question?" in result
    assert "*2.* Second question?" in result


def test_format_string_options():
    questions = [
        {
            "question": "Pick one:",
            "options": ["alpha", "beta", "gamma"],
        }
    ]
    result = SlackAdapter._format_questions_for_slack(questions)
    assert "`alpha`" in result
    assert "`beta`" in result
    assert "`gamma`" in result
