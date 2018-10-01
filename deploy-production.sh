#!/bin/sh
trap 'exit' ERR
../../firi/google_appengine/appcfg.py update app.yaml
