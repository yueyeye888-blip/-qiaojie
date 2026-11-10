#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
/usr/bin/env python3 restart_source.py
result=$?
if (( result != 0 )); then
  read -r '?重启未完成。请保留上方提示，按回车关闭。'
fi
exit $result
