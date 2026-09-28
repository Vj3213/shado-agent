<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.shado.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>__PROJECT__/.venv/bin/python</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>agent.app.main:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8100</string>
  </array>
  <key>WorkingDirectory</key><string>__PROJECT__</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>__LOGS__/agent.log</string>
  <key>StandardErrorPath</key><string>__LOGS__/agent.log</string>
</dict>
</plist>
