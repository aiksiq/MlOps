import json, re, wave, subprocess
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from build_demo import ROOT, scenes, font, wrap

def stamp(t):
    ms=round(t*1000); return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
timeline=[]; subs=[]; count=1; now=0; script=['# HW11 — сценарий и источники','', 'Русский синтезированный голос: Microsoft Irina. Материалы проекта показаны как сохранённые доказательства; команды запуска — инструкция, а не запись нового эксперимента.','']
for i,s in enumerate(scenes):
    with wave.open(str(ROOT/f'voice-{i:02}.wav')) as w: duration=w.getnframes()/w.getframerate()/1.65+1.2
    sentences=re.split(r'(?<=[.!?])\s+',s['narration'])
    parts=[]
    for sentence in sentences:
        words=sentence.split(); chunk=''
        for word in words:
            if len(chunk+' '+word)>108:
                parts.append(chunk); chunk=word
            else: chunk=(chunk+' '+word).strip()
        if chunk: parts.append(chunk)
    elapsed=0
    for part in parts:
        span=(duration-1.2)*len(part)/sum(len(p) for p in parts)
        subs.append(f'{count}\n{stamp(now+elapsed)} --> {stamp(now+elapsed+span)}\n'+ '\n'.join(wrap(part,1600,font(33)))+'\n')
        count+=1; elapsed+=span
    script += [f'## {stamp(now)[:-4]} — {s["title"]}', '', '**Экран:** '+s['body'].replace('\n',' / '), '', '**Диктор:** '+s['narration'],'', '**Источник:** '+s['source'],'']
    timeline.append({'index':i,'start':now,'duration':duration}); now+=duration
(ROOT/'subtitles.srt').write_text('\n'.join(subs),encoding='utf-8')
(ROOT/'scenario.md').write_text('\n'.join(script),encoding='utf-8')
(ROOT/'timeline.json').write_text(json.dumps(timeline,indent=2),encoding='utf-8')
for t in timeline:
    i=t['index']; dur=t['duration']
    args=['ffmpeg','-v','error','-y','-loop','1','-framerate','15','-i',str(ROOT/f'slide-{i:02}.png'),'-i',str(ROOT/f'voice-{i:02}.wav'),'-vf',f'fade=t=in:st=0:d=0.35,fade=t=out:st={dur-.35}:d=0.35','-af','atempo=1.65,apad','-t',str(dur),'-c:v','libx264','-preset','ultrafast','-crf','23','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k',str(ROOT/f'part-{i:02}.mp4')]
    subprocess.run(args,check=True); print('Encoded',i,flush=True)
(ROOT/'concat.txt').write_text('\n'.join(f"file 'part-{i:02}.mp4'" for i in range(len(scenes))),encoding='utf-8')
subprocess.run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',str(ROOT/'concat.txt'),'-i',str(ROOT/'subtitles.srt'),'-map','0:v','-map','0:a','-map','1:0','-c','copy','-c:s','mov_text','-metadata:s:s:0','language=rus','-disposition:s:0','default','-movflags','+faststart',str(ROOT/'hw11-demo-ru.mp4')],check=True)
print('DONE',now,flush=True)
