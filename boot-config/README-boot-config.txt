BOOT PARTITION CONFIGURATION FILES
====================================

After flashing Raspberry Pi OS Lite to your microSD card, copy these files
to the "bootfs" partition (the small FAT32 partition that shows up in Windows).

Files to copy:
  - ssh                 -> Enables SSH on first boot (empty file)
  - userconf.txt        -> Sets default username/password
  - config.txt.append   -> Camera settings to APPEND to the existing config.txt

userconf.txt format:
  username:hashed_password
  Generate the hash with:  openssl passwd -6 yourpassword

IMPORTANT: For config.txt, do NOT replace the existing file. Instead, APPEND
the contents of config.txt.append to the END of the existing config.txt file
on the boot partition.

WiFi will be configured by the setup script after first boot via ethernet
or by using Raspberry Pi Imager's advanced settings (recommended).

RECOMMENDED APPROACH:
  Use Raspberry Pi Imager's advanced settings (gear icon) to configure:
    - Hostname: picam
    - Username + Password (choose your own; do NOT reuse common ones)
    - WiFi SSID + Password
    - Enable SSH with password authentication
    - Locale: your timezone

  This is the easiest method and handles everything automatically.
