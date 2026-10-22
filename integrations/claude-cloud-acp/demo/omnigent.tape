# Omnigent REPL -> ACP bridge -> Claude Code cloud session: create, tool call, follow-up.
# vhs omnigent.tape -o "$tmp/omnigent.mp4"   (run from a trusted git checkout's parent)
Set Shell bash
Set FontSize 14
Set Width 1600
Set Height 1200
Set TypingSpeed 25ms

Hide
Type "cd ~/dev/ccr-spike-repo && export PS1='> ' && clear" Enter
Show

Type "cat ~/dev/claude-cloud-agent/config.yaml; git log --oneline -1" Sleep 300ms Enter
Sleep 3s
Type "omnigent run ~/dev/claude-cloud-agent" Sleep 300ms Enter
Wait+Screen@90s /ready/
Sleep 2s
Type "Run `pwd; git log --oneline -1; uname -sm` and report the output." Sleep 300ms Enter
Wait+Screen@300s /◆/
Sleep 15s
Type "Follow-up: how many commits does the repo have? Answer with one word in capitals." Sleep 300ms Enter
Wait+Screen@300s /(?s)◆.*◆/
Sleep 15s
Ctrl+C
Sleep 2s
