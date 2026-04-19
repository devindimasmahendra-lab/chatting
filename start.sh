#!/usr/bin/env bash
gunicorn binmas:app --worker-class gevent --workers 1
