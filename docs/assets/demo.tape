# VHS tape for the README demo GIF (#209).
# Re-record with:  vhs docs/assets/demo.tape
# (run from a directory where `kvm-pilot` is on PATH and profile `homelab`
#  points at a live GLKVM; the final screen-frame splice is done in ffmpeg —
#  see the recipe at the bottom.)

Output demo-terminal.mp4

Set FontSize 20
Set Width 1200
Set Height 600
Set Padding 24
Set TypingSpeed 40ms

Hide
Type "export PATH=$DEMO_PATH:$PATH && clear"
Enter
Sleep 500ms
Show

Type "kvm-pilot snapshot screen.jpg --profile homelab"
Sleep 500ms
Enter
# preflight + on-demand encoder wake + save takes several seconds live
Sleep 11s

Type "kvm-pilot power-cycle --profile homelab --dry-run"
Sleep 500ms
Enter
Sleep 6s

Sleep 1s

# Post-processing recipe (ffmpeg + gifsicle):
#   ffmpeg -loop 1 -t 3 -i screen.jpg -vf "scale=1200:600:force_original_aspect_ratio=decrease,pad=1200:600:(ow-iw)/2:(oh-ih)/2:color=black" -r 25 -pix_fmt yuv420p frame.mp4
#   printf "file 'demo-terminal.mp4'\nfile 'frame.mp4'\n" > list.txt
#   ffmpeg -f concat -safe 0 -i list.txt -c:v libx264 -pix_fmt yuv420p joined.mp4
#   ffmpeg -i joined.mp4 -vf "fps=10,scale=960:-1:flags=lanczos,palettegen" palette.png
#   ffmpeg -i joined.mp4 -i palette.png -filter_complex "fps=10,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse" demo.gif
#   gifsicle -O3 --lossy=80 demo.gif -o docs/assets/demo.gif
