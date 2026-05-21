#!/bin/bash
pkill -f pyTREMOR_lights01 2>/dev/null
pkill -f UNDERWATER_LIGTHING_TREMOR 2>/dev/null
sleep 2
nohup setsid /home/sjc1/venv_tremor/bin/python3 /home/sjc1/pyTREMOR_lights01.py >/tmp/tremor.log 2>&1 </dev/null &
disown
echo "launched pid=$!"
