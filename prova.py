
import re

reg = r'!time\s?\((.*?)\)([(]([A-Za-z]+/[A-Za-z]+|UTC)[)])?'
# reg = r'!time\s?\((.*?)\)'

print(re.findall(reg, '!time(13:40)'))