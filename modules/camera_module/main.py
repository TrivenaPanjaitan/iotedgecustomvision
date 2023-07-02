from azure.iot.device.aio import IoTHubModuleClient
from flask import Flask, render_template, Response, jsonify, request
from multiprocessing import Process, Queue
import asyncio
import cv2
import flask
import json
import logging
import pyodbc
import requests
import socket
import threading
import time

conn_str = 'Driver={ODBC Driver 18 for SQL Server};Server=tcp:iotedgecustomvisionserver.database.windows.net,1433;Database=iotedgecustomvisiondatabase;Uid=iotedgecustomvisionadmin;Pwd=P@ssw0rd;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;'
conn = pyodbc.connect(conn_str)

app = Flask(__name__)

def flask_run(frame_queue):
    app.run(host='0.0.0.0', port="5000")


@app.route('/')
def index():
    return render_template('index.html',)

@app.route('/search', methods=['GET','POST'])
def search():
    if request.method == 'POST':
        keyword = request.form['keyword']
        query = f"SELECT IR.NIM, SD.Name, SD.ProgramStudy, IR.EventProcessedUtcTime, IR.Status FROM InspectionResults IR JOIN StudentData SD ON IR.NIM = SD.NIM WHERE IR.NIM LIKE '%{keyword}%' ORDER BY IR.EventProcessedUtcTime DESC"
        results = execute_query(query)
        return render_template('results.html', results=results)
    query = f"SELECT IR.NIM, SD.Name, SD.ProgramStudy, IR.EventProcessedUtcTime, IR.Status FROM InspectionResults IR JOIN StudentData SD ON IR.NIM = SD.NIM ORDER BY IR.EventProcessedUtcTime DESC"
    results = execute_query(query)
    return render_template('index.html', results=results)

def gen():
    while True:
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_queue.get() + b'\r\n\r\n')


@app.route('/video_feed')
def video_feed():
    return Response(gen(),
                mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/data_feed')
def data_feed():
    # Fetch data from Azure SQL Database
    cursor = conn.cursor()
    cursor.execute("SELECT TOP 20 IR.NIM, SD.Name, SD.ProgramStudy, IR.EventProcessedUtcTime, IR.Status FROM InspectionResults IR JOIN StudentData SD ON IR.NIM = SD.NIM ORDER BY IR.EventProcessedUtcTime DESC")
    data = cursor.fetchall()

    # Convert data to a list of dictionaries
    columns = [column[0] for column in cursor.description]
    result = []
    for row in data:
        result.append(dict(zip(columns, row)))

    return jsonify(result)


async def camera(frame_queue):
    headers = {'Content-Type': 'application/octet-stream'}

    # get ip adedress of other container
    tensorflow_container_ip=socket.gethostbyname('model_module')
    tensorflow_container_ip='http://'+tensorflow_container_ip+':80'+'/image'

    # get camera frame
    cap = cv2.VideoCapture(0)
    
    module_client = IoTHubModuleClient.create_from_edge_environment()
    
    await module_client.connect()

    while(True):
        # Capture frame-by-frame
        ret, frame = cap.read()

        height, width, channels = frame.shape

        ret, encodedFrame=cv2.imencode(".jpg",frame)

        try:
            response = requests.post(url=tensorflow_container_ip, headers = headers, data = encodedFrame.tostring())
            response_json = response.json()
            response_json = response_json["predictions"]

            json_output={}
            for box in response_json:
                if float(box["probability"]) > 0.6:
                    x1=int(float(box["boundingBox"]["left"])*width)
                    y1=int(float(box["boundingBox"]["top"])*height)
                    x2=int(x1+float(box["boundingBox"]["width"])*width)
                    y2=int(y1+float(box["boundingBox"]["height"])*height)
                    
                    cv2.rectangle(frame,(x1,y1),(x2,y2),(255,0,0),2)
                    text=box["tagName"]+' '+str(round(box["probability"],2))
                    cv2.putText(frame,text,(x2,y2),cv2.FONT_HERSHEY_SIMPLEX,0.7,(0,0,255),2)


                    json_output["NIM"]=box["tagName"]
                    json_output["Probability"]=box["probability"]
            
            ret, encodedFrame2=cv2.imencode(".jpg",frame)
            frame_byte=encodedFrame2.tobytes()
            
            if int(frame_queue.qsize()) > 2:
                frame_queue.get()
                frame_queue.put(frame_byte)
            else:
                frame_queue.put(frame_byte)
            
            if len(json_output) != 0:
                await module_client.send_message_to_output(json.dumps(json_output), "output")
            
            json_output={}

        except Exception as error:
            logging.error("Error from camera code: %s", str(error))
            await module_client.send_message_to_output('something wrong from camera code', "output")
            time.sleep(2)

def execute_query(query):
    with conn.cursor() as cursor:
        cursor.execute(query)
        results = cursor.fetchall()
    return results

if __name__ == '__main__':
    frame_queue=Queue()

    camera_process = Process(target=flask_run, args=(frame_queue,))
    camera_process.start()

    asyncio.run(camera(frame_queue))