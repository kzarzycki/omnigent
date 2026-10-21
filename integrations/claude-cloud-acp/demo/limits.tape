# The bridge's known limits, each driven by e2e.py against real cloud sessions.
Set Shell bash
Set FontSize 14
Set Width 1600
Set Height 1000
Set TypingSpeed 20ms

Hide
Type "cd ~/dev/ext/omnigent/worktrees/claude-cloud-acp/integrations/claude-cloud-acp && export PS1='> ' && clear" Enter
Show

Type "python3 e2e.py untrusted" Sleep 300ms Enter
Wait@300s
Sleep 4s
Type "clear; python3 e2e.py cancel ~/dev/ccr-spike-repo" Sleep 300ms Enter
Wait@600s
Sleep 4s
Type "clear; python3 e2e.py restart ~/dev/ccr-spike-repo" Sleep 300ms Enter
Wait@600s
Sleep 4s
Type "clear; python3 e2e.py subagent ~/dev/ccr-spike-repo" Sleep 300ms Enter
Wait@600s
Sleep 5s
