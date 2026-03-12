[app]
title = Stock Analyzer Pro
package.name = stockswinganalyzer
package.domain = com.indianstocks
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
version = 1.0.0

# Critical requirements for Android
# Note: pandas and numpy require specific p4a recipes
requirements = python3,kivy==2.2.1,yfinance,pandas,numpy,requests,urllib3,certifi,charset-normalizer,idna,websocket-client,plyer

android.permissions = INTERNET, ACCESS_NETWORK_STATE, WRITE_EXTERNAL_STORAGE

# Android API levels
android.api = 33
android.minapi = 21
android.ndk = 25b
android.sdk = 33
android.arch = arm64-v8a

# UI Configuration
orientation = portrait
fullscreen = 0
android.presplash_color = #040812

[buildozer]
log_level = 2
warn_on_root = 1
