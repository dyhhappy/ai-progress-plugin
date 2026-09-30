"""Read only explicit public commentary for one hook-selected Codex turn."""
import json
from pathlib import Path


class PublicStages:
    def __init__(self,path,turn_id):
        self.path=Path(path)
        self.turn_id=turn_id
        self.offset=0
        self.discard_line=False

    def events(self):
        try:
            with self.path.open('rb') as stream:
                stream.seek(self.offset)
                for _ in range(256):
                    line=stream.readline(1024*1024+1)
                    if not line:break
                    if len(line)>1024*1024 or self.discard_line:
                        self.offset=stream.tell()
                        self.discard_line=not line.endswith(b'\n')
                        continue
                    if not line.endswith(b'\n'):break
                    self.offset=stream.tell()
                    if len(line)>1024*1024:continue
                    try:record=json.loads(line)
                    except (ValueError,UnicodeError):continue
                    if not isinstance(record,dict):continue
                    payload=record.get('payload',{})
                    if record.get('type')!='response_item' or not isinstance(payload,dict):continue
                    if payload.get('role')!='assistant' or payload.get('type')!='message':continue
                    if (payload.get('phase') or payload.get('channel'))!='commentary':continue
                    meta=payload.get('internal_chat_message_metadata_passthrough') or {}
                    if not isinstance(meta,dict) or meta.get('turn_id')!=self.turn_id:continue
                    content=payload.get('content',[])
                    if not isinstance(content,list):continue
                    text=' '.join(x.get('text','') for x in content if isinstance(x,dict) and x.get('type')=='output_text' and isinstance(x.get('text'),str)).strip()
                    if text:
                        yield {'type':'activity','turn_id':self.turn_id,'message':text[:240],'source':'public-commentary'}
        except OSError:return
