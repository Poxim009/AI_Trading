#!/bin/sh
set -e

start_vnc() {
  log_info "Starting x11vnc on port $VNC_PORT..."
  if [ -n "$UI_PASSWORD" ]; then
    x11vnc -display :0 -forever -rfbport $VNC_PORT -passwd "$UI_PASSWORD" &
  else
    x11vnc -display :0 -forever -rfbport $VNC_PORT -nopw &
  fi
  X11VNC_PID=$!

  sleep 1
}

start_websockify() {
  log_info "Starting unified noVNC Web UI + WebSocket proxy on port $NOVNC_PAGE_PORT..."
  echo "{\"port\": $NOVNC_PORT, \"host\": \"$NOVNC_HOST\"}" >/opt/noVNC/defaults.json
  echo "{\"port\": $NOVNC_PORT, \"host\": \"$NOVNC_HOST\"}" >/opt/noVNC/mandatory.json
  ln -sf /opt/noVNC/vnc.html /opt/noVNC/index.html
  if [ ! -x /usr/bin/websockify ]; then
    apk add --no-cache python3 py3-pip >/dev/null 2>&1 && pip install --break-system-packages websockify >/dev/null 2>&1 || true
  fi
  /usr/bin/websockify --web=/opt/noVNC 0.0.0.0:$NOVNC_PAGE_PORT 127.0.0.1:$VNC_PORT &
  NOVNC_PID=$!
  sleep 1
}

start_novnc_http() {
  log_info "noVNC HTTP + WebSocket unified on port $NOVNC_PAGE_PORT via websockify (PID: $NOVNC_PID)."
  NOVNC_PAGE_PID=$NOVNC_PID
  sleep 1
}
