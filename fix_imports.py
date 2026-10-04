with open('EasyAudioConverter.py', 'r', encoding='utf-8-sig') as f:
    text = f.read()

text = text.replace('import time', 'import time\\nimport datetime')
text = text.replace('from concurrent.futures import ThreadPoolExecutor', 'from concurrent.futures import ThreadPoolExecutor, as_completed')

with open('EasyAudioConverter.py', 'w', encoding='utf-8-sig') as f:
    f.write(text)
