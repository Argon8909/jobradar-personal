"""Local-only conversational interface. Do not expose this unauthenticated app to network."""
import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from flask import Flask, jsonify, render_template, request

import orchestrator
import storage

app=Flask(__name__)
app.json.ensure_ascii=False
app.config['MAX_CONTENT_LENGTH']=6*1024*1024
orchestrator.bootstrap()


@app.get('/')
def home():
    return render_template('index.html')


@app.get('/api/state')
def state():
    return jsonify({'task':storage.last_task(),'counts':storage.counts(),
                    'messages':storage.messages(50),'events':storage.events(limit=35),
                    'results':storage.get_analyses(25)})


@app.post('/api/chat')
def chat():
    data=request.get_json(silent=True) or {}
    try:
        return jsonify(orchestrator.chat(data.get('message','')))
    except Exception as exc:
        return jsonify({'error':str(exc)[:400]}),400


def extract_resume(file):
    name=Path(file.filename or '').name
    suffix=Path(name).suffix.lower()
    raw=file.read(5*1024*1024+1)
    if len(raw)>5*1024*1024:raise ValueError('Размер файла превышает 5 МБ')
    if suffix in ('.txt','.md'):
        return raw.decode('utf-8-sig')
    if suffix=='.docx':
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml=z.read('word/document.xml')
        root=ElementTree.fromstring(xml)
        ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        return '\n'.join(''.join(t.text or '' for t in p.iter(ns+'t')) for p in root.iter(ns+'p'))
    if suffix=='.pdf':
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError('Для PDF установи зависимости: py -m pip install -r requirements.txt') from exc
        pdf=PdfReader(io.BytesIO(raw))
        if len(pdf.pages)>30:raise ValueError('Резюме PDF должно быть не длиннее 30 страниц')
        return '\n'.join(page.extract_text() or '' for page in pdf.pages)
    raise ValueError('Поддерживаются TXT, MD, DOCX, PDF')


@app.post('/api/resume')
def upload_resume():
    upload=request.files.get('file')
    if not upload:return jsonify({'error':'Файл не выбран'}),400
    try:
        content=extract_resume(upload).strip()
        if not content:raise ValueError('В файле нет доступного текста; сканы PDF не поддерживаются')
        if len(content)>60000:raise ValueError('Текст резюме слишком длинный (максимум 60000 символов)')
        resume_id=storage.store_resume(Path(upload.filename).name,content)
        storage.log('resume',f'Загружено резюме: {Path(upload.filename).name}')
        return jsonify({'resume_id':resume_id,'filename':Path(upload.filename).name})
    except Exception as exc:
        return jsonify({'error':str(exc)[:300]}),400


@app.get('/api/health')
def health():
    return jsonify({'ok':True})


if __name__=='__main__':
    print('JobRadar запущен: http://127.0.0.1:8765')
    app.run(host='127.0.0.1',port=8765,debug=False,use_reloader=False,threaded=True)
