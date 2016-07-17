from cortex import app
import cortex.lib.core
import cortex.lib.systems
from flask import Flask, request, session, redirect, url_for, flash, g, abort, make_response, jsonify, Response
import os 
import re
import MySQLdb as mysql
import yaml

################################################################################

@app.route('/api/systems/csv')
def api_systems_csv():
	"""Returns a CSV file, much like the /systems/download/csv but with API
	auth rather than normal auth."""

	# The request should contain a parameter in the headers which contains
	# the authentication pre-shared key. Validate this:
	if 'X-Auth-Token' not in request.headers:
		app.logger.warn('auth_token missing from Systems API request')
		return abort(401)
	if request.headers['X-Auth-Token'] != app.config['CORTEX_API_AUTH_TOKEN']:
		app.logger.warn('Incorrect auth_token on request to Systems API')
		return abort(401)

	# Get the list of systems
	cur = cortex.lib.systems.get_systems(return_cursor=True)

	# Return the response as a downloadable CSV
	return Response(cortex.lib.systems.csv_stream(cur), mimetype="text/csv", headers={'Content-Disposition': 'attachment; filename="systems.csv"'})
	
################################################################################

@app.route('/api/puppet/enc/<certname>')
def api_puppet_enc(certname):
	"""Returns the YAML associated with the given node."""

	# The request should contain a parameter in the headers which contains
	# the autthentication pre-shared key. Validate this:
	if 'X-Auth-Token' not in request.headers:
		app.logger.warn('auth_token missing from Puppet ENC API request (certname: ' + certname + ')')
		return abort(401)
	if request.headers['X-Auth-Token'] != app.config['ENC_API_AUTH_TOKEN']:
		app.logger.warn('Incorrect auth_token on request to Puppet ENC API (certname: ' + certname + ')')
		return abort(401)

	# Check that we've got a valid hostname
	if not cortex.lib.core.is_valid_hostname(certname):
		app.logger.warn('Invalid certname presented to Puppet ENC API (certname: ' + certname + ')')
		abort(400)

	# Generate the Puppet configuration
	node_yaml = cortex.lib.puppet.generate_node_config(certname)

	# If we don't get any configuration, return 404
	if node_yaml is None:
		return abort(404)

	# Make a response and return it
	r = make_response(node_yaml)
	r.headers['Content-Type'] = "application/x-yaml"
	return r

################################################################################

@app.route('/api/puppet/hiera/node/<certname>')
def api_puppet_hiera_pernode(certname):
	"""Returns the Hiera YAML associated with the given node."""

	# The request should contain a parameter in the headers which contains
	# the autthentication pre-shared key. Validate this:
	if 'X-Auth-Token' not in request.headers:
		app.logger.warn('auth_token missing from Puppet Hiera Node API request (certname: ' + certname + ')')
		return abort(401)
	if request.headers['X-Auth-Token'] != app.config['ENC_API_AUTH_TOKEN']:
		app.logger.warn('Incorrect auth_token on request to Puppet Hiera Node API (certname: ' + certname + ')')
		return abort(401)

	# Check that we've got a valid hostname
	if not cortex.lib.core.is_valid_hostname(certname):
		app.logger.warn('Invalid certname presented to Puppet Hiera Node API (certname: ' + certname + ')')
		abort(400)

	# Get the YAML
	yaml = cortex.lib.puppet.get_node_hiera(certname)

	# If we don't get any configuration, return 404
	# if we return a blank document then hiera-http on the puppet master
	# explodes with a hilarious nonsensical error.
	if yaml is None:
		return abort(404)

	# Make a response and return it
	r = make_response(yaml)
	r.headers['Content-Type'] = "application/x-yaml"
	return r

################################################################################

@app.route('/api/puppet/hiera/role/<name>')
def api_puppet_hiera_perrole(certname):
	"""Returns the Hiera YAML associated with the given role."""

	# The request should contain a parameter in the headers which contains
	# the autthentication pre-shared key. Validate this:
	if 'X-Auth-Token' not in request.headers:
		app.logger.warn('auth_token missing from Puppet Hiera Role API request (certname: ' + certname + ')')
		return abort(401)
	if request.headers['X-Auth-Token'] != app.config['ENC_API_AUTH_TOKEN']:
		app.logger.warn('Incorrect auth_token on request to Puppet Hiera Role API (certname: ' + certname + ')')
		return abort(401)

	# Get the YAML
	yaml = cortex.lib.puppet.get_role_hiera(rolename)

	# If we don't get any configuration, return 404
	# if we return a blank document then hiera-http on the puppet master
	# explodes with a hilarious nonsensical error.
	if yaml is None:
		return abort(404)

	# Make a response and return it
	r = make_response(yaml)
	r.headers['Content-Type'] = "application/x-yaml"
	return r
