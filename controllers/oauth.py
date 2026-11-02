import secrets
from datetime import datetime
from odoo import fields, http
from odoo.http import request
from werkzeug.exceptions import BadRequest, Forbidden


class OAuth(http.Controller):
    @http.route('/elks/qbo/callback', type='http', auth='user', methods=['GET'])
    def callback(self, code=None, state=None, realmId=None, **kwargs):
        pending = request.session.pop('elks_qbo_oauth', None)
        if (not pending or not state or not secrets.compare_digest(state, pending['state'])
                or pending['uid'] != request.env.uid
                or datetime.fromisoformat(pending['expires']) < fields.Datetime.now()):
            raise Forbidden('Invalid or expired QuickBooks authorization state.')
        if not request.env.user.has_group('base.group_system'):
            raise Forbidden()
        if not code or not realmId or not realmId.isdigit():
            raise BadRequest('QuickBooks authorization was cancelled or invalid.')
        conn = request.env['elks.qbo.connection'].browse(pending['connection']).exists()
        if not conn:
            raise BadRequest()
        conn._lock()
        if conn.realm_id and conn.realm_id != realmId:
            raise BadRequest('A different QuickBooks company needs a new connection and mapping review.')
        conn._token({'grant_type': 'authorization_code', 'code': code, 'redirect_uri': conn.redirect_uri})
        conn.realm_id = realmId
        return request.redirect('/odoo')
