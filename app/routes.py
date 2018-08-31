from flask import jsonify, request
from flask import render_template

from app import app
from app.models import Device


@app.route('/')
@app.route('/index')
def index():
    user = {'username': 'Benjamin'}
    return render_template('index.html', title='Home', user=user)

def get_device(identifier):
    return Device.objects(vendor_identifier=identifier)

@app.route('/device', methods=['GET'])
def get_devices():
    devices = Device.objects

    return jsonify(devices)

@app.route('/device/<identifier>', methods=['GET'])
def device(identifier):
    device = get_device(identifier)

    return jsonify(device)

@app.route('/device/<identifier>', methods=['POST','PUT'])
def set_device(identifier):
    device = Device()

    if request.method == 'POST':
        device = get_device(identifier).first()

    data = request.get_json()

    device.name = data["name"]
    device.save()

    return jsonify(device)
