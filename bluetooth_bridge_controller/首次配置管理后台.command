#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
/usr/bin/env python3 setup_local.py
result=$?
if (( result != 0 )); then
  read -r '?配置未完成。请保留上方提示，按回车关闭。'
fi
exit $result
