# dbus-mpp-p18

mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
docker build -t inverter-tools-arm .
docker run --rm -it inverter-tools-arm   bash
