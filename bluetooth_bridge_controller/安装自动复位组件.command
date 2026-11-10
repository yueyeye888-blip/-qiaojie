#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
/usr/bin/env python3 enable_source_reset.py
result=$?
if (( result != 0 )); then
  read -r '?配置未完成，请保留具体错误，按回车关闭。'
else
  read -r '?自动复位已启用，请保持两台试点在线，等待管理台验收。按回车关闭。'
fi
exit $result
