"""Font-independent native Tk bitmap-style icons (no Pillow dependency)."""


def icon_image(tk, master, key, color, scale=1.0):
    size = max(12, round(16 * scale))
    image = tk.PhotoImage(master=master, width=size, height=size)
    def rect(x1,y1,x2,y2):
        image.put(color, to=(round(x1*size/16), round(y1*size/16),
                             round(x2*size/16), round(y2*size/16)))
    if key in ("dot", "ring"):
        for y in range(3,13):
            for x in range(3,13):
                r=(x-7.5)**2+(y-7.5)**2
                if r <= 25 and (key == "dot" or r >= 12):
                    rect(x,y,x+1,y+1)
    elif key == "pause":
        rect(4,3,6,13); rect(10,3,12,13)
    elif key == "resume":
        for x in range(4,12):
            spread=(12-x)//2
            rect(x,8-spread,x+1,9+spread)
    elif key == "stop":
        rect(4,4,12,12)
    elif key in ("expand", "collapse"):
        for x,y in ((2,2),(10,2),(2,10),(10,10)):
            rect(x,y,x+4,y+1); rect(x,y,x+1,y+4)
    else:
        for i in range(3,13):
            rect(i,i,i+1,i+1); rect(i,15-i,i+1,16-i)
    return image
