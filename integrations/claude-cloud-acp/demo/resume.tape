# Resume, archive, cancel-then-kill and sub-agents, each driven by e2e.py against real cloud sessions.
Set Shell bash
Set FontSize 14
Set Width 1600
Set Height 1000
Set TypingSpeed 20ms

Hide
Type "export PS1='demo$ ' && clear" Enter
Show

Type `python3 e2e.py restart "$DEMO_CHECKOUT"` Sleep 300ms Enter
Wait@600s /^demo\$$/
Sleep 4s
Type `clear; python3 e2e.py respawn "$DEMO_CHECKOUT"` Sleep 300ms Enter
Wait@600s /^demo\$$/
Sleep 4s
Type `clear; python3 e2e.py archived "$DEMO_CHECKOUT"` Sleep 300ms Enter
Wait@600s /^demo\$$/
Sleep 4s
Type `clear; python3 e2e.py teardown "$DEMO_CHECKOUT"` Sleep 300ms Enter
Wait@600s /^demo\$$/
Sleep 4s
Type `clear; python3 e2e.py subagent "$DEMO_CHECKOUT"` Sleep 300ms Enter
Wait@600s /^demo\$$/
Sleep 5s
