import json
import os
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
HERMETIC_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")} | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

HEREDOC_READS = {
    "heredoc_script": "cat > helper.sh <<'SH'\ngh pr comment 1 --body clean\nSH",
    "heredoc_document": "cat <<'EOF' > instructions.txt\ngit push https://github.com/owner/project.git main\nEOF",
    "heredoc_python": "python3 - <<'PY'\ntext = '''\ngh pr comment 1 --body clean\n'''\nprint(text)\nPY",
    "heredoc_double_quote": 'cat <<"EOF"\n$(gh pr comment 1 --body clean)\nEOF',
    "heredoc_escaped_delimiter": "cat <<\\EOF\ngh pr comment 1 --body clean\nEOF",
    "heredoc_partial_quote": "cat <<E'O'F\ngh pr comment 1 --body clean\nEOF",
    "heredoc_tab_strip": "cat <<-'EOF'\n\tgh pr comment 1 --body clean\n\tEOF\ngh pr list",
    "heredoc_multiple": "cat <<'ONE' <<'TWO'\ngh pr comment 1 --body clean\nONE\ngit push origin main\nTWO\ngit status",
    "heredoc_continued": "cat <<\\\n'EOF'\ngh pr comment 1 --body clean\nEOF",
}
GIT_READS = {
    "git_glob_pathspecs": "git --glob-pathspecs diff -- src/*.py",
    "git_noglob_pathspecs": "git --noglob-pathspecs status --short",
    "git_icase_pathspecs": "git --icase-pathspecs ls-files readme.md",
    "git_no_lazy_fetch": "git --no-lazy-fetch status --short",
    "git_html_path": "git --html-path",
    "git_man_path": "git --man-path",
    "git_info_path": "git --info-path",
    "git_no_advice": "git --no-advice status --short",
    "git_future_read_option": "git --future-option status --short",
    "git_read_operand_push": "git --glob-pathspecs log --grep push",
}
GH_READS = {
    "gh_completion": "gh completion -s bash",
    "gh_completion_long": "gh completion --shell bash",
    "gh_browse": "gh browse --no-browser",
    "gh_browse_path": "gh browse README.md --no-browser",
    "gh_browse_short": "gh browse -n",
    "graphql_explicit_POST": "gh api graphql -X POST -f 'query=query { viewer { login } }'",
    "graphql_method_long": "gh api graphql --method=POST --raw-field 'query={ viewer { login } }'",
    "graphql_method_attached": "gh api graphql -XPOST -f 'query=query($n:Int!){viewer{login}}' -F n=1",
    "graphql_default_POST": "gh api graphql -f 'query=query { viewer { login } }'",
    "rest_GET_with_fields": "gh api -X GET search/issues -f q=label:bug",
}
HEREDOC_WRITES = {
    "write_after_heredoc": "cat <<'EOF'\nclean\nEOF\ngh pr comment 1 --body clean",
    "write_on_declaration_line": "cat <<'EOF'; gh pr comment 1 --body clean\nclean\nEOF",
    "write_in_pipeline": "cat <<'EOF' | gh api repos/o/r/issues --input -\n{}\nEOF",
    "heredoc_inside_string": 'echo "cat <<\'EOF\'"\ngh pr comment 1 --body clean\nEOF',
    "heredoc_in_comment": "# cat <<'EOF'\ngh pr comment 1 --body clean\nEOF",
    "here_string": "cat <<< 'EOF'\ngh pr comment 1 --body clean\nEOF",
}
GIT_WRITES = {
    "unknown_option_push": "git --future-option push https://github.com/owner/project.git main",
    "global_option_push": "git --glob-pathspecs push https://github.com/owner/project.git main",
}
GH_WRITES = {
    "graphql_POST_mutation": "gh api graphql -X POST -f 'query=mutation { test }'",
    "graphql_default_mutation": "gh api graphql -f 'query=mutation { test }'",
    "graphql_POST_mixed": "gh api graphql -X POST -f 'query=query { viewer { login } } mutation { test }'",
    "graphql_POST_duplicate_query": "gh api graphql -X POST -f 'query=mutation { test }' -f 'query=query { viewer { login } }'",
    "graphql_PATCH_query": "gh api graphql -X PATCH -f 'query=query { viewer { login } }'",
    "rest_POST_with_fields": "gh api repos/o/r/issues -X POST -f body=clean",
}


class HookReadRegressions(unittest.TestCase):
    def check_cases(self, cases, expected):
        for name, shell in cases.items():
            with self.subTest(name=name):
                event = {"tool_name": "Bash", "cwd": str(ROOT), "tool_input": {"command": shell}}
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(ROOT / "bin/outbound-guard")],
                    input=json.dumps(event), capture_output=True, text=True, timeout=7, env=HERMETIC_ENV,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(result.stderr.splitlines()), int(bool(expected)))

    def test_quoted_heredocs_are_data(self):
        self.check_cases(HEREDOC_READS, 0)

    def test_non_heredoc_shell_syntax_stays_read_only(self):
        self.check_cases({
            "arithmetic_shift": "echo $((1 << 2))",
            "arithmetic_command": "((1 << 2))",
            "here_string": "cat <<< 'gh pr comment 1 --body clean'",
            "quoted_redirect_text": "echo \"cat <<'EOF'\"",
            "comment_redirect_text": "# cat <<'EOF'\ngit status",
            "unquoted_parameter": "cat <<EOF\n${HOME}\nEOF",
        }, 0)

    def test_git_global_options_on_reads(self):
        self.check_cases(GIT_READS, 0)

    def test_gh_local_commands_and_graphql_POST_reads(self):
        self.check_cases(GH_READS, 0)

    def test_heredocs_do_not_hide_writes(self):
        self.check_cases(HEREDOC_WRITES, 2)

    def test_unknown_git_options_do_not_hide_pushes(self):
        self.check_cases(GIT_WRITES, 2)

    def test_graphql_mutations_and_ambiguous_payloads_are_denied(self):
        self.check_cases(GH_WRITES, 2)


if __name__ == "__main__":
    unittest.main()
