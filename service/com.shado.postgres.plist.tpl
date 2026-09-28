<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.shado.postgres</string>
  <key>ProgramArguments</key>
  <array>
    <string>__PG_BIN__/postgres</string>
    <string>-D</string>
    <string>__PG_DATA__</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>LC_ALL</key><string>C</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>__LOGS__/postgres.log</string>
  <key>StandardErrorPath</key><string>__LOGS__/postgres.log</string>
</dict>
</plist>
