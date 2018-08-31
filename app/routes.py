from flask import render_template
from app import app
from app import models
from flask import jsonify


@app.route('/')
@app.route('/index')
def index():
    user = {'username': 'Benjamin'}
    return render_template('index.html', title='Home', user=user)

@app.route('/device', methods=['GET'])
def get_devices():
    devices = models.Device.objects

    return jsonify(devices)