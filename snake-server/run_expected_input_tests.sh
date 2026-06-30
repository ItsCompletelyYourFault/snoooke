#!/usr/bin/env bash
set -euo pipefail
python3 -m py_compile server.py server_input_test_utils.py test_server_expected_inputs.py test_server_debug_chat_flood.py
python3 test_server_expected_inputs.py
python3 test_server_debug_chat_flood.py
