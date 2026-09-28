<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.shado.gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>__NODE_BIN__/node</string>
    <string>__GATEWAY__/node_modules/tsx/dist/cli.mjs</string>
    <string>src/index.ts</string>
  </array>
  <key>WorkingDirectory</key><string>__GATEWAY__</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>__LOGS__/gateway.log</string>
  <key>StandardErrorPath</key><string>__LOGS__/gateway.log</string>
</dict>
</plist>
