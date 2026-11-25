from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

chat_started = threading.Event()
chat_release = threading.Event()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, data):
        encoded=json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def do_GET(self):
        if self.path == '/test/chat-status':
            self.send({'started':chat_started.is_set()}); return
        if self.path != '/api/tags': self.send_error(404); return
        self.send({'models':[{'name':'all-minilm:latest','digest':'a'*64}]})
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/test/release':
            chat_release.set(); self.send({'released':True}); return
        if self.path == '/test/reset':
            chat_started.clear(); chat_release.clear(); self.send({'reset':True}); return
        if self.path == '/api/embed':
            self.send({'embeddings':[[1.0]+[0.0]*383 for _ in data['input']]})
            return
        if self.path == '/api/chat':
            if 'allowed' in data['format'].get('properties', {}):
                content={'allowed':True}
            else:
                payload=json.loads(data['messages'][-1]['content'])
                sources=payload['sources']
                kinds={source['kind'] for source in sources}
                if not {'approved_reference','self_reported_patient_profile'}.issubset(kinds):
                    self.send_error(400); return
                if not any('Synthetic patient history' in source['text'] for source in sources if source['kind']=='self_reported_patient_profile'):
                    self.send_error(400); return
                if any('Synthetic unapproved reference' in source['text'] for source in sources):
                    self.send_error(400); return
                if payload['question'] in ('Explain consent race','Explain retirement race'):
                    chat_started.set()
                    if not chat_release.wait(20):
                        self.send_error(504); return
                content={'answer':'Synthetic educational explanation; verify with a clinician.', 'citations':[source['id'] for source in sources if source['kind'] in ('approved_reference','self_reported_patient_profile')]}
            self.send({'message':{'content':json.dumps(content)}})
            return
        self.send_error(404)
ThreadingHTTPServer(('0.0.0.0',11434),Handler).serve_forever()
