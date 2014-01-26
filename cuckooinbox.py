#!/usr/bin/env python
# coding: utf-8
import os
import sys
import logging
import argparse
import string
import re
import time
import imaplib
import smtplib
import zipfile
import tempfile
import threading

logging.basicConfig()

sys.path.append(os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))

import email
import quopri
from imapclient import IMAPClient
from bs4 import BeautifulSoup
from email.MIMEMultipart import MIMEMultipart
from email.MIMEBase import MIMEBase
from email.MIMEText import MIMEText
from email.Utils import COMMASPACE, formatdate
from email import Encoders
from email import Charset

from lib.cuckoo.common.constants import CUCKOO_ROOT, CUCKOO_VERSION
from lib.cuckoo.common.exceptions import CuckooStartupError
from lib.cuckoo.common.config import Config
from lib.cuckoo.common.colors import *
from lib.cuckoo.core.database import Database
from lib.cuckoo.common.abstracts import Report
from lib.cuckoo.common.exceptions import CuckooReportError
from lib.cuckoo.common.objects import File


class CuckooRequest(object):

    def __init__(self, message):

        self.message = message
        
        '''cuckooinbox config variables'''
        config = Config(cfg=os.path.join(CUCKOO_ROOT,"cuckooinbox","cuckooinbox.conf"))
        config = config.get('cuckooinbox')
        self.username = config['username']
        self.passwd = config['passwd']
        self.imap = config['imap']
        self.imap_ssl = config['imap_ssl']
        self.smtp_server = config['smtp']
        self.interval = config['interval']
        self.email_whitelist = config['email_whitelist']
        self.url_limit = config['url_limit']
        self.attachment_limit = config['attachment_limit'] 
        self.zip_reports = config['zip_reports']
        self.zip_password = config['zip_password']
        self.url_blacklist = config['url_blacklist']
        self.url_file_backlist = config['url_file_backlist']
        self.machine = config['machine']
        
        '''imap variables'''
        self.server = IMAPClient(self.imap, use_uid=True, ssl=self.imap_ssl)
        self.server.login(self.username, self.passwd)
        self.attachment_counter = 0
        
        '''message variables'''
        self.msg = MIMEMultipart()
        self.response_msg = MIMEMultipart()
        self.response_urls = []
        self.response_attachments = []
        self.sender = ''
        self.subject = ''
        self.cc_list = []

        '''logging object'''
        self.log_entry = Logger('cuckooinbox.log')

        '''cuckoo variables'''
        self.taskids = []
        self.db  = Database()
        self.url_counter = 0 # tracks url count to not exceed url_limit
   
 
    def fetch(self, message):
        
        '''set retrieve folder'''
        select_info = self.server.select_folder('INBOX')
        '''fetch mail'''
        response = self.server.fetch(self.message, ['RFC822'])
    
        '''parse received email'''
        for msgid, data in response.iteritems():
            msg_string = data['RFC822']
            self.msg = email.message_from_string(msg_string)
            
            '''parse 'Name <user@address.com>' format'''
            if '<' in self.msg['From']: self.sender = self.msg['From'].split('<'[0])[-1][:-1]            
            else: self.sender = self.msg['From']
            self.subject = self.msg['Subject']
            
            '''print and log successful receive'''
            self.log_entry.logEvent('[+] Received email ID: %d from %s [%s]' % (msgid, self.msg['From'], self.msg['Subject']))
            
            '''save CC info for reply later'''
            if self.msg['Cc']:
                for address in  self.msg['Cc'].split(', '): self.cc_list.append(address)
                self.log_entry.logEvent( '[*] Email \"%s\" from %s cc\'d the following addresses: %s' % (self.msg['Subject'],self.msg['From'],', '.join(str(copies) for copies in self.cc_list)))
            
            file_whitelist = ['exe', 'doc', 'docx', 'xls', 'xlsx', 'pdf', 'zip']
            
            '''parse message elements'''
            for part in self.msg.walk():

                if part.get_content_type() == 'text/plain':
                    self.log_entry.logEvent( '[*] Email ID: %d has a plain text object.' % msgid)
                    content = part.get_payload()
                    self.processPlainText(content)

                elif part.get_content_type() == 'text/html':
                    self.log_entry.logEvent('[*] Email ID: %d has a html object.' % msgid)
                    content = part.get_payload()
                    self.processText(content)

                elif 'application' in part.get_content_type():
                    # email attachment has no filename
                    if not part.get_param('name'): return 0
                    # cuckoo file analysis whitelist
                    if not part.get_param('name').split('.'[0])[-1] in file_whitelist: break
                    # increment and break if limit is reached 
                    if (self.attachment_limit != 0 and self.attachment_counter == self.attachment_limit): break
                    self.attachment_counter += 1
                    self.log_entry.logEvent('[*] Email ID: %d has an attachment object.' % msgid)
                    content = part.get_payload()
                    file_name = part.get_param('name')
                    self.processAttachment(content, file_name)
                    

    def processPlainText(self, content):

	url_list = re.findall('http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', content)

	for url in url_list:

            # strip blacklist links and filetypes
	    if url in self.url_blacklist.split(','): return 0
	    if url in self.url_file_backlist.split(','): return 0

            self.response_urls.append(url)

            if self.machine:
                task_id = self.db.add_url(url, package="ie", timeout=15, machine=self.machine)
            else: task_id = self.db.add_url(url, package="ie", timeout=15)
            if task_id:
                self.taskids.append(task_id)
                self.log_entry.logEvent('[+] URL \"%s\" added as task with ID %d' % (url, task_id))
                # increment counter and exit loop if limit is reached
                self.url_counter += 1
                if (self.url_limit != 0 and self.url_counter == self.url_limit): return 0
            else:
                self.log_entry.logEvent("[!] Error: adding task to database" % (url, task_id))
                break	

		
    def processText(self, content):

        '''reformat quoted string mail to plain html'''
        body = quopri.decodestring(content)
        soup = BeautifulSoup(body)
        # todo analyze href spoof
	
        '''parse and analyze hyperlinks'''
        for url in soup.findAll('a'):
            # strip mailto links
            if url['href'].split(':'[0])[0] == 'mailto' : continue
	    url = url['href'] 

            # strip blacklist links and filetypes
	    if url in self.url_blacklist.split(','): return 0 
	    if url in self.url_file_backlist.split(','): return 0

            else:
                self.response_urls.append(url)
                if self.machine:
                    task_id = self.db.add_url(url, package="ie", timeout=15, machine=self.machine)
                else: task_id = self.db.add_url(url, package="ie", timeout=15)
                if task_id:
                    self.taskids.append(task_id)
                    self.log_entry.logEvent('[+] URL \"%s\" added as task with ID %d' % (url, task_id))
                    # increment counter and exit loop if limit is reached
                    self.url_counter += 1
                    if (self.url_limit != 0 and self.url_counter == self.url_limit): return 0
                else:
                    self.log_entry.logEvent("[!] Error: adding task to database" % (url, task_id))
                    break


    def processAttachment(self, content, filename):

        '''create temp file for analysis'''
        temp_file = tempfile.NamedTemporaryFile(prefix=filename.split('.'[0])[0], suffix='.' + filename.split('.'[0])[1])
        temp_file.write(content)

        '''add to cuckoo tasks'''
        task_id = self.db.add_path(temp_file.name, timeout=10, package=filename.split('.'[0])[1])
        if task_id:
            self.taskids.append(task_id)
            self.log_entry.logEvent('[+] File \"%s\" added as task with ID %d' % (filename,task_id))
        else:
            self.taskids.append(task_id)
            self.log_entry.logEvent("[!] Error adding task to database")
	    return 0
            
        '''make sure file gets submitted before we toss it'''
        timeout = time.time() + 120
        while time.time() < timeout:
            if os.path.exists(os.path.join(CUCKOO_ROOT,"storage","analyses",str(task_id),"reports","report.html")): continue
            time.sleep(.25)

	self.response_attachments.append(filename)
	temp_file.flush()
        temp_file.close()


    def zipResults(self,):
        
        '''create temporary zip file'''
        temp_zip = tempfile.TemporaryFile(prefix='report',suffix='.zip')
        zip_file = zipfile.ZipFile(temp_zip, 'w')
        if self.zip_password: zip_file.setpassword(self.zip_password)
        
        '''set zip to compress'''
        try:
            import zlib
            compression = zipfile.ZIP_DEFLATED
        except:
            compression = zipfile.ZIP_STORED
        modes = { zipfile.ZIP_DEFLATED: 'deflated',
                zipfile.ZIP_STORED:   'stored',}
        
        '''wait for reports to finish then add to list'''
        for id in self.taskids:
            # timeout error handling
            if not os.path.exists(os.path.join(CUCKOO_ROOT,"storage","analyses",str(id),"reports","report.html")):
                self.log_entry.logEvent('cuckooinbox error: report timeout reached on task ID %d.' % id)
            else: 
                zip_file.write(os.path.join(CUCKOO_ROOT,"storage","analyses",str(id),"reports","report.html"),\
                arcname = 'report' + str(id) + '.html', compress_type=compression)
        zip_file.close()
            
        '''attach zip to email message'''
        temp_zip.seek(0)
        email_file = MIMEBase('application', 'zip')
        email_file.set_payload(temp_zip.read())
        Encoders.encode_base64(email_file)
        email_file.add_header('Content-Disposition', 'attachment; filename="report.zip"')
        self.response_msg.attach(email_file)


    def sendReport(self,):

        '''create email header'''
        assert type(self.cc_list)==list
        assert type(self.taskids)==list
        self.response_msg['From'] = self.username
        self.response_msg['To'] = self.sender
        self.response_msg['Cc'] = ", ".join(self.cc_list)
        self.response_msg['Date'] = formatdate(localtime=True)
        self.response_msg['Subject'] = 'cuckooinbox report: ' + self.subject

        '''attach cuckooinbox email body'''
        for id in self.taskids:
            '''wait for reports to finish before sending'''
            timeout = time.time() + 120
            while time.time() < timeout:
                if os.path.exists(os.path.join(CUCKOO_ROOT,"storage","analyses",str(id),"reports","report.html")): continue
                time.sleep(.25)
            if os.path.exists(os.path.join(CUCKOO_ROOT,"storage","analyses",str(id),"reports","inbox.html")):
                file = open(os.path.join(CUCKOO_ROOT,"storage","analyses",str(id),"reports","inbox.html"))
                body = '<html>' + \
                    '<div class="section-title">'+ \
                    '<h2>Task ID %d <small></small></h2>' % id + \
                    '</div>'+ \
                    '<table class="table table-striped table-bordered">'+ \
                    file.read() + \
                    '</html>'
                file.close()
                response_text = ''.join(body)
                self.response_msg.attach(MIMEText(response_text,'html'))
            else: print '[!] Could not find cuckoobox report files.'

        '''wait for analysis to finish and zip the reports'''
        self.zipResults()
        
        '''send the message'''
        if '@gmail.com' in self.username:
            smtp = smtplib.SMTP('smtp.gmail.com',587)
            smtp.starttls()
            smtp.login(self.username, self.passwd)
        else:
            smtp = smtplib.SMTP(self.smtp_server)
            try: smtp.login(self.username,self.passwd)
            except:
                self.log_entry.logEvent('[!] SMTP login failed.')
        try: smtp.sendmail(self.username, self.sender, self.response_msg.as_string())
        except:
            self.log_entry.logEvent('SMTP message %s failed to send.' % self.subject)
        smtp.close()
        self.log_entry.logEvent('[-] Sent "%s" report to %s' % (self.subject, self.sender))
        self.server.logout()


class Logger(object):
    
    def __init__(self,log_file):
        self.log_file = log_file
            
    def logEvent(self, log_entry):
        print log_entry
        log = open(self.log_file,'a')
        timestamp = time.asctime(time.localtime())
        data = timestamp + ' ' + log_entry + '\n'
        log.write(data)
    
    def emailLog(self, username, sender, subject, text):
        logEvent('cuckooinbox.log', '[!] %s' % text)
        body = string.join((
                "From: %s" % username,
                "To: %s" % sender,
                "Subject: cuckooinbox error: %s" % subject,
                "", text), "\r\n")
        smtp = smtplib.SMTP(self.smtp_server)
        smtp.login(username,passwd)
        smtp.sendmail(username, sender, body)
        smtp.close()


def main():
        
    def checkConfigs():
        '''check for config file and define variables'''
        config = os.path.join(CUCKOO_ROOT,"cuckooinbox","cuckooinbox.conf")
        if not os.path.exists(config):
            raise CuckooStartupError("Config file does not exist at path: %s" % config)
    
    checkConfigs()
    config = Config(cfg=os.path.join(CUCKOO_ROOT,"cuckooinbox","cuckooinbox.conf"))
    config = config.get('cuckooinbox')
    username = config['username']
    passwd = config['passwd']
    imap = config['imap']
    imap_ssl = config['imap_ssl']
    email_whitelist = config['email_whitelist']
    interval = config['interval']
    
    '''welcome screen'''
    print '\n\n'
    print '\t\t@\tsend your malware to %s !\n' % (username)
    welcome_message = '           _,\n         ((\')\n        /\'--)\n        | _.\'\n       / |`=\n      \'^\''
    print welcome_message

    '''thread main function'''        
    def analyze(message):
        request = CuckooRequest(message)
        request.fetch(message)
        request.sendReport()
    
    '''define imap connection'''
    server = IMAPClient(imap, use_uid=True, ssl=imap_ssl)
    
    '''connect, login'''
    server.login(username, passwd)
   
    while True:
        try:
            '''set retrieve folder'''
            select_info = server.select_folder('INBOX')
            '''search for new message from email whitelist'''
            for account in email_whitelist.split(','):
                messages = server.search('UNSEEN FROM "%s"' % account)
                '''analyze emails from one account at a time'''
                if messages:
                    for message in messages:
                        thread = threading.Thread( target = analyze, args = (message,))
			thread.daemon = True
                        thread.start()
            time.sleep(interval)
        except:
            '''reconnect to mail account'''
            server = IMAPClient(imap, use_uid=True, ssl=imap_ssl)
            server.login(username, passwd)
            pass
        
if __name__ == "__main__":
    main()
    
