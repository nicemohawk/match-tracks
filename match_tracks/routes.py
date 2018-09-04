from flask import jsonify, request, abort, render_template
from flask_httpauth import HTTPTokenAuth
from marshmallow import ValidationError

from match_tracks import app
from match_tracks.models import Device, DeviceSchema, SessionSchema

device_schema = DeviceSchema()
auth = HTTPTokenAuth(scheme='APIKey')


tokens = {
    "hi-bob": "bob"
}

# token verification method
@auth.verify_token
def verify_token(token):
    if token in tokens:
        # g.current_user = tokens[token]
        return True
    return False


@app.route('/')
@app.route('/index/')
def index():
    user = {'username': 'Human'}
    return render_template('index.html', title='Home', user=user)


# READ all devices
@app.route('/devices/', methods=['GET'])
def get_devices():
    devices = Device.objects

    if devices.count() == 0:
        abort(404)

    result, error = device_schema.dump(devices, many=True)

    return jsonify({'devices': result})


# CREATE single device
@app.route('/devices/', methods=['POST'])
@auth.login_required
def create_device():
    json_data = request.get_json()

    if not json_data:
        return jsonify({'message': 'No input data provided.'}), 400

    # Validate and deserialize input
    try:
        created_device, errors = device_schema.load(json_data)
    except ValidationError as err:
        return jsonify(err.messages), 422

    created_device.save()

    return jsonify({'device': created_device})


# READ single device
@app.route('/devices/<uuid:identifier>/', methods=['GET'])
def get_device(identifier):
    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    result, error = device_schema.dump(device)

    return jsonify({'device': result})


# UPDATE single device by vendor_identifier
@app.route('/devices/<uuid:identifier>/', methods=['PUT'])
def update_device(identifier):
    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    json_data = request.get_json()

    if not json_data:
        return jsonify({'message': 'No input data provided.'}), 400

    # Validate and deserialize input
    try:
        updated_device, errors = device_schema.update(device, json_data)
    except ValidationError as err:
        return jsonify(err.messages), 422

    updated_device.save()

    return jsonify({'updated_device': device_schema.dump(updated_device)})


@app.route('/devices/<uuid:identifier>/', methods=['DELETE'])
def delete_device(identifier):
    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    response = {'deleted_device': device_schema.dump(device).data}

    try:
        device.delete()
    except Exception as err:
        return jsonify(type(err).__name__), 422

    return jsonify(response)


@app.route('/devices/<identifier>/sessions/', methods=['GET'])
def get_device_sessions(identifier):
    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    result, error = SessionSchema().dump(device.sessions, many=True)

    return jsonify({'sessions': result})


@app.route('/devices/<identifier>/sessions/', methods=['POST'])
def add_session(identifier):
    json_data = request.get_json()

    device = Device.objects(vendor_identifier=str(identifier)).first_or_404()

    sessions = device.sessions

    try:
        new_sessions, errors = SessionSchema().load(json_data, many=True)
    except ValidationError as err:
        return jsonify(err.messages), 422

    for session in new_sessions:
        sessions.append(session)

    sessions.save()
    result, error = SessionSchema().dump(new_sessions, many=True)

    return jsonify({'added_sessions': result})

