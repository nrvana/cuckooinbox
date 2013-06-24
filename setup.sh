#!/bin/sh
# cuckooinbox reporting setup script

cp -v reporting/reportinbox.py ../modules/reporting/
cp -v reporting/base-email.html ../data/html/
cp -v reporting/base-email-item.html ../data/html/
cp -v reporting/inbox.html ../data/html/inbox.html
cp -v reporting/network-brief.html ../data/html/sections/

# echo cmd to edit conf/reporting.conf
echo "" >> ../conf/reporting.conf
echo "[reportinbox]" >> ../conf/reporting.conf
echo "enabled = on" >> ../conf/reporting.conf
