# Copyright (c) LinkedIn Corporation. All rights reserved. Licensed under the BSD-2 Clause license.
# See LICENSE in the project root for license information.

from __future__ import absolute_import

import logging
import time
import hmac
import hashlib
import base64
from streql import equals
import importlib
from falcon import HTTPUnauthorized, HTTPForbidden, Request
from .. import db

logger = logging.getLogger('oncall.auth')
auth_manager = None
app_key_cache = {}


def debug_only(function):
    def wrapper(*args, **kwargs):
        raise HTTPForbidden('', 'Admin only action')
    return wrapper


def is_god(challenger):
    connection = db.connect()
    cursor = connection.cursor()
    cursor.execute('SELECT `id` FROM `user` WHERE `god` = TRUE AND `name` = %s', challenger)
    is_god = cursor.rowcount
    cursor.close()
    connection.close()
    return is_god != 0


def check_user_auth(user, req):
    """
    Check to see if current user is user or admin of team where user is in
    """
    if 'app' in req.context:
        return
    challenger = req.context['user']
    if user == challenger:
        return
    connection = db.connect()
    cursor = connection.cursor()
    get_allowed_query = '''SELECT DISTINCT(`user`.`name`)
        FROM `team_admin`
        JOIN `team_user` ON `team_admin`.`team_id` = `team_user`.`team_id`
        JOIN `user` ON `user`.`id` = `team_user`.`user_id`
        JOIN `user` AS `admin` ON `admin`.`id` = `team_admin`.`user_id`
        WHERE `admin`.`name` = %s'''
    cursor.execute(get_allowed_query, challenger)
    allowed = (user,) in cursor
    cursor.close()
    connection.close()
    if allowed or is_god(challenger):
        return
    raise HTTPForbidden('Unauthorized', 'Action not allowed for "%s"' % challenger)


def check_team_auth(team, req):
    """
    Check to see if the current user is admin of the team
    """
    if 'app' in req.context:
        return
    challenger = req.context['user']
    connection = db.connect()
    cursor = connection.cursor()
    get_allowed_query = '''SELECT `team`.`name`
                           FROM `team_admin`
                           JOIN `team` ON `team_admin`.`team_id` = `team`.`id`
                           JOIN `user` ON `team_admin`.`user_id` = `user`.`id`
                           WHERE `user`.`name` = %s'''
    cursor.execute(get_allowed_query, challenger)
    allowed = (team,) in cursor
    cursor.close()
    connection.close()
    if allowed or is_god(challenger):
        return
    raise HTTPForbidden(
        'Unauthorized',
        'Action not allowed: "%s" is not an admin for "%s"' % (challenger, team))


def check_calendar_auth(team, req, user=None):
    if 'app' in req.context:
        return
    challenger = user if (user is not None) else req.context['user']
    connection = db.connect()
    cursor = connection.cursor()
    cursor.execute('''SELECT `user`.`name`
        FROM `team_user`
        JOIN `user` ON `team_user`.`user_id` = `user`.`id`
        WHERE `team_user`.`team_id` = (SELECT `id` FROM `team` WHERE `name` = %s)
            AND `user`.`name` = %s''', (team, challenger))
    user_in_team = cursor.rowcount
    cursor.close()
    connection.close()
    if user_in_team != 0 or is_god(challenger):
        return
    raise HTTPForbidden('Unauthorized', 'Action not allowed: "%s" is not part of "%s"' % (challenger, team))


def check_calendar_auth_by_id(team_id, req):
    if 'app' in req.context:
        return
    challenger = req.context['user']
    query = '''SELECT `user`.`name`
               FROM `team_user`
               JOIN `user` ON `team_user`.`user_id` = `user`.`id`
               WHERE `team_user`.`team_id` = %s
               AND `user`.`name` = %s'''
    connection = db.connect()
    cursor = connection.cursor()
    cursor.execute(query, (team_id, challenger))
    user_in_team = cursor.rowcount
    cursor.close()
    connection.close()
    if user_in_team != 0 or is_god(challenger):
        return
    raise HTTPForbidden('Unauthorized', 'Action not allowed: "%s" is not a team member' % (challenger))


def is_client_digest_valid(client_digest, api_key, window, method, path, body):
    text = '%s %s %s %s' % (window, method, path, body)
    HMAC = hmac.new(api_key, text, hashlib.sha512)
    digest = base64.urlsafe_b64encode(HMAC.digest())
    if equals(client_digest, digest):
        return True
    return False


def authenticate_application(auth_token, req):
    if not auth_token.startswith('hmac '):
        raise HTTPUnauthorized('Authentication failure', 'Invalid digest format', '')
    method = req.method
    path = req.env['PATH_INFO']
    qs = req.env['QUERY_STRING']
    if qs:
        path = path + '?' + qs
    body = req.context['body']
    try:
        app_name, client_digest = auth_token[5:].split(':', 1)
        if app_name not in app_key_cache:
            connection = db.connect()
            cursor = connection.cursor()
            cursor.execute('SELECT `key` FROM `application` WHERE `name` = %s', app_name)
            if cursor.rowcount > 0:
                app_key_cache[app_name] = cursor.fetchone()[0]
                cursor.close()
                connection.close()
            else:
                cursor.close()
                connection.close()
                raise HTTPUnauthorized('Authentication failure', 'Application not found', '')
        api_key = str(app_key_cache[app_name])
        window = int(time.time()) // 5
        if is_client_digest_valid(client_digest, api_key, window, method, path, body):
            req.context['app'] = app_name
            return
        elif is_client_digest_valid(client_digest, api_key, window - 1, method, path, body):
            req.context['app'] = app_name
            return
        else:
            raise HTTPUnauthorized('Authentication failure', 'Wrong digest', '')
    except (ValueError, KeyError):
        raise HTTPUnauthorized('Authentication failure', 'Wrong digest', '')


def _authenticate_user(req):
    session = req.env['beaker.session']
    try:
        req.context['user'] = session['user']

        connection = db.connect()
        cursor = connection.cursor()

        cursor.execute('SELECT `csrf_token` FROM `session` WHERE `id` = %s', session['_id'])
        if cursor.rowcount != 1:
            cursor.close()
            connection.close()
            raise HTTPUnauthorized('Invalid Session', 'CSRF token missing', '')

        token = cursor.fetchone()[0]
        if req.get_header('X-CSRF-TOKEN') != token:
            cursor.close()
            connection.close()
            raise HTTPUnauthorized('Invalid Session', 'CSRF validation failed', '')

        cursor.close()
        connection.close()
    except KeyError:
        raise HTTPUnauthorized('Unauthorized', 'User must be logged in', '')


authenticate_user = _authenticate_user


def login_required(function):
    def wrapper(*args, **kwargs):
        for i, arg in enumerate(args):
            if isinstance(arg, Request):
                idx = i
                break
        req = args[idx]
        auth_token = req.get_header('AUTHORIZATION')
        if auth_token:
            authenticate_application(auth_token, req)
        else:
            authenticate_user(req)
        return function(*args, **kwargs)

    return wrapper


def init(application, config):
    global check_team_auth
    global check_user_auth
    global check_calendar_auth
    global check_calendar_auth_by_id
    global debug_only
    global auth_manager
    global authenticate_user

    if config.get('debug', False):
        def authenticate_user_test_wrapper(req):
            try:
                _authenticate_user(req)
            except HTTPUnauthorized:
                # avoid login for e2e tests
                req.context['user'] = 'test_user'

        logger.info('Auth debug turned on.')
        authenticate_user = authenticate_user_test_wrapper
        check_team_auth = lambda x, y: True
        check_user_auth = lambda x, y: True
        check_calendar_auth = lambda x, y, **kwargs: True
        check_calendar_auth_by_id = lambda x, y: True
        debug_only = lambda function: function

    if config.get('docs'):
        # Replace login_required decorator with identity function for autodoc generation
        global login_required
        login_required = lambda x: x
    else:
        connection = db.connect()
        cursor = connection.cursor()
        cursor.execute('SELECT `name`, `key` FROM `application`')
        for row in cursor:
            app_key_cache[row[0]] = row[1]
        cursor.close()
        connection.close()
        logger.debug('loaded applications: %s', app_key_cache.keys())

        auth = importlib.import_module(config['module'])
        auth_manager = getattr(auth, 'Authenticator')(config)

    from . import login, logout
    application.add_route('/login', login)
    application.add_route('/logout', logout)
