# Without `os_env.sandbox.type: none` the default seatbelt sandbox blocks the pty the bridge needs.
Set Shell bash
Set FontSize 15
Set Width 1600
Set Height 700
Set TypingSpeed 25ms

Hide
Type "cd ~/dev/ccr-spike-repo && export PS1='> ' && clear" Enter
Show

Type "cat ~/dev/claude-cloud-agent-sandboxed/config.yaml" Sleep 300ms Enter
Sleep 3s
Type "omnigent run ~/dev/claude-cloud-agent-sandboxed -p 'Say hi.'" Sleep 300ms Enter
Wait+Screen@180s /pty|rror/
Sleep 6s
Ctrl+C
Sleep 1s
