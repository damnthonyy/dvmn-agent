from pr_agent.agent.pr_agent import command2class, commands, unknown_auto_commands


class TestUnknownAutoCommands:
    def test_known_commands_pass(self):
        assert unknown_auto_commands(["/describe", "/improve", "/review"]) == []

    def test_known_command_with_args_is_stripped_to_the_action(self):
        assert unknown_auto_commands(
            ["/describe --pr_description.publish_description_as_comment=true"]
        ) == []

    def test_leading_slash_and_case_are_normalized(self):
        assert unknown_auto_commands(["improve", "/IMPROVE", "/Describe"]) == []

    def test_unknown_command_is_reported(self):
        assert unknown_auto_commands(["/agentic_review"]) == ["agentic_review"]

    def test_mixed_list_reports_only_the_unknown(self):
        assert unknown_auto_commands(
            ["/improve", "/agentic_review", "/describe", "/does_not_exist"]
        ) == ["agentic_review", "does_not_exist"]

    def test_empty_and_none_inputs(self):
        assert unknown_auto_commands([]) == []
        assert unknown_auto_commands(None) == []

    def test_every_registered_command_is_accepted(self):
        assert unknown_auto_commands([f"/{name}" for name in commands]) == []
        assert set(commands) == set(command2class.keys())
