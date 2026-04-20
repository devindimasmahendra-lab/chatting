#!/usr/bin/env bash
gunicorn chatting:app --worker-class gevent --workers 1
