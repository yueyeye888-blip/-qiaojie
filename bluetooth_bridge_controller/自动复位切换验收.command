#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
/usr/bin/env python3 source_cycle_acceptance.py
result=$?
if (( result != 0 )); then
  read -r '?验收未通过。请保留上方提示，按回车关闭。'
else
  read -r '?自动复位验收结束。请保留结果，按回车关闭。'
fi
exit $result
