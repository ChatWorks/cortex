#!/usr/bin/python
#

from cortex import app
import cortex.core
from flask import Flask, request, session, redirect, url_for, flash, g, abort, make_response, render_template, jsonify
import os 
import re
import MySQLdb as mysql

import time
from multiprocessing import Process, Value
import syslog

################################################################################
#### HOME PAGE / LOGIN PAGE

@app.route('/', methods=['GET', 'POST'])
def login():
	if cortex.core.is_user_logged_in():
		return redirect(url_for('dashboard'))
	else:
		if request.method == 'GET' or request.method == 'HEAD':
			next = request.args.get('next',default=None)
			return render_template('login.html', next=next)

		elif request.method == 'POST':
			result = cortex.core.auth_user(request.form['username'], request.form['password'])

			if not result:
				flash('Incorrect username and/or password','alert-danger')
				return redirect(url_for('login'))
			
			## Set the username in the session
			session['username']  = request.form['username'].lower()
			
			## Check if two-factor is enabled for this account
			## TWO STEP LOGONS
			if app.config['TOTP_ENABLED']:
				if cortex.totp.totp_user_enabled(session['username']):
					app.logger.debug('User "' + session['username'] + '" has two step enabled. Redirecting to two-step handler')
					return redirect(url_for('totp_logon_view',next=request.form.get('next',default=None)))

			## Successful logon without 2-step needed
			return cortex.core.logon_ok()


################################################################################
#### LOGOUT

@app.route('/logout')
@cortex.core.login_required
def logout():
	## Log out of the session
	cortex.core.session_logout()
	
	## Tell the user
	flash('You were logged out successfully','alert-success')
	
	## redirect the user to the logon page
	return redirect(url_for('login'))

#### HELP PAGES
@app.route('/about')
def about():
	return render_template('about.html', active='help')

@app.route('/about/changelog')
def changelog():
	return render_template('changelog.html', active='help')

@app.route('/nojs')
def nojs():
	return render_template('nojs.html')

@app.route('/dashboard')
def dashboard():
	"""This renders the front page after the user logged in."""

	# Get a cursor to the database
	cur = g.db.cursor(mysql.cursors.DictCursor)
	
	# Get number of VMs
	cur.execute('SELECT COUNT(*) AS `count` FROM `vmware_cache_vm`');
	row = cur.fetchone()
	vm_count = row['count']

	# Get number of in-progress tasks
	cur.execute('SELECT COUNT(*) AS `count` FROM `tasks` WHERE `status` = %s', (0,))
	row = cur.fetchone()
	task_progress_count = row['count']

	# Get number of failed tasks in the last 3 hours
	cur.execute('SELECT COUNT(*) AS `count` FROM `tasks` WHERE `status` = %s AND `end` > DATE_SUB(NOW(), INTERVAL 3 HOUR)', (2,))
	row = cur.fetchone()
	task_failed_count = row['count']

	return render_template('dashboard.html', vm_count=vm_count, task_progress_count=task_progress_count, task_failed_count=task_failed_count)

################################################################################

def render_task_status(id, template):
	"""The task_status and task_status_log functions do /very/ similar
	things. This function does that work, and is herely purely to reduce
	code duplication."""

	# Get a cursor to the database
	cur = g.db.cursor(mysql.cursors.DictCursor)

	# Get the task
	cur.execute("SELECT `id`, `module`, `username`, `start`, `end`, `status` FROM `tasks` WHERE id = %s", (id,))
	task = cur.fetchone()

	# Get the events for the task
	cur.execute("SELECT `id`, `source`, `related_id`, `name`, `username`, `desc`, `status`, `start`, `end` FROM `events` WHERE `related_id` = %s AND `source` = 'neocortex.task'", (id,))
	events = cur.fetchall()

	return render_template(template, id=id, task=task, events=events)


@app.route('/task/status/<int:id>', methods=['GET'])
@cortex.core.login_required
def task_status(id):
	"""Handles the Task Status page for a individual task."""

	return render_task_status(id, "task-status.html")

@app.route('/task/status/<int:id>/log', methods=['GET'])
@cortex.core.login_required
def task_status_log(id):
	"""Much like task_status, but only returns the event log. This is used by 
	an AJAX routine on the page to refresh the log every 10 seconds."""

	return render_task_status(id, "task-status-log.html")

@app.route('/codemirror')
def codemirror_test():
	return render_template('codemirror.html', active='help')

