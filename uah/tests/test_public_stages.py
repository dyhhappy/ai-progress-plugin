import json
from pathlib import Path
import tempfile
import unittest
from uah.adapters.public_stages import PublicStages


class PublicStageTests(unittest.TestCase):
    def test_only_current_turn_public_commentary_and_partial_writes(self):
        def record(phase,turn,text):
            return json.dumps({'type':'response_item','payload':{'type':'message','role':'assistant',
                'phase':phase,'internal_chat_message_metadata_passthrough':{'turn_id':turn},
                'content':[{'type':'output_text','text':text}]}},ensure_ascii=False).encode()+b'\n'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'session.jsonl'
            path.write_bytes(record('analysis','now','private')+record('final_answer','now','answer')+
                record('commentary','old','old turn')+record('commentary','now','当前阶段：正在制作 PPT'))
            reader=PublicStages(path,'now')
            self.assertEqual([e['message'] for e in reader.events()],['当前阶段：正在制作 PPT'])
            self.assertEqual(list(reader.events()),[])
            line=record('commentary','now','正在检查排版')
            with path.open('ab') as stream:stream.write(line[:-1])
            self.assertEqual(list(reader.events()),[])
            with path.open('ab') as stream:stream.write(b'\n')
            self.assertEqual([e['message'] for e in reader.events()],['正在检查排版'])

if __name__=='__main__':unittest.main()
