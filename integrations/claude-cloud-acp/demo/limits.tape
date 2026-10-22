# A folder Claude Code has not trusted fails clearly; driven by e2e.py without starting a cloud session.
Set Shell bash
Set FontSize 14
Set Width 1600
Set Height 1000
Set TypingSpeed 20ms

Hide
Type "cd ~/dev/ext/omnigent/worktrees/claude-cloud-acp/integrations/claude-cloud-acp && export PS1='demo$ ' && clear" Enter
Show

Type "python3 e2e.py untrusted" Sleep 300ms Enter
Wait@300s /^demo\$$/
Sleep 5s
