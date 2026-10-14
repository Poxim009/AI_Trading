#!/bin/sh
sed -i 's/Enabled=0/Enabled=1/' /app/src/config.sh
sed -i 's/mkfifo -m 666/rm -f \$WIN_ROOT\/server \&\& mkfifo -m 666/' /app/src/automation.sh
FIRST_RUN=1 MT5_LOGIN=${MT5_LOGIN:-113645327} MT5_PASSWORD=${MT5_PASSWORD} MT5_SERVER=${MT5_SERVER:-MetaQuotes-Demo} MT5=/opt/wineprefix/drive_c/MT5 sh -c ". /app/src/config.sh; apply_mt5_config"
