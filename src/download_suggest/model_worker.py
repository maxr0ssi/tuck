"""Isolated model process: receives excerpts and choices, never file operations."""
import contextlib
import json
import os
import sys


def main():
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY','1')
    with contextlib.redirect_stdout(sys.stderr):
        import tinyjev
        agent=tinyjev.load('TinyJev-0.6B',quantize=8)
        agent.predict({'state':'An example document.','questions':{'folder':{'type':'choice','instructions':'Choose a destination.','criteria':{'example':'Example documents','leave':'Unknown'}}}})
    print(json.dumps({'ready':True}),flush=True)
    for line in sys.stdin:
        request={}
        try:
            request=json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                result=agent.predict(request['request'])
            answers=result.get('answers')
            if answers is None:
                answers=result.get('states',[{}])[0].get('answers',{})
            print(json.dumps({'id':request['id'],'answers':answers}),flush=True)
        except Exception:
            print(json.dumps({'id':request.get('id'),'error':'Model could not classify this item.'}),flush=True)

if __name__=='__main__':
    main()
