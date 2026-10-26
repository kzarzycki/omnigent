# Without `os_env.sandbox.type: none` the default seatbelt sandbox blocks the pty the bridge needs.
Set Shell bash
Set FontSize 15
Set Width 1600
Set Height 700
Set TypingSpeed 25ms

Hide
Type `cd "$DEMO_CHECKOUT" && export PS1='> ' && clear` Enter
Show

Type `cat "$DEMO_AGENT_SANDBOXED/config.yaml"` Sleep 300ms Enter
Sleep 3s
Type `omnigent run "$DEMO_AGENT_SANDBOXED" -p 'Say hi.'` Sleep 300ms Enter
Wait+Screen@180s /pty|rror/
Sleep 6s
Ctrl+C
Sleep 1s
