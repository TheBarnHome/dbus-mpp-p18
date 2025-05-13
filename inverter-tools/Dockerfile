FROM --platform=linux/arm/v7 debian:bullseye

# Mise à jour et installation des outils
RUN apt-get update && apt-get install -y \
    nano \
    git \
    cmake \
    make \
    build-essential \
    libhidapi-hidraw0 \
    libhidapi-libusb0 \
    libhidapi-dev \
    libserialport-dev \
    libudev-dev \
    && apt-get clean

# Clonage du dépôt
RUN git clone https://github.com/gch1p/inverter-tools.git /opt/inverter-tools

# Modifier le CMakeLists.txt pour utiliser les libs statiques
COPY CMakeLists.txt /opt/inverterd

# Compilation
WORKDIR /opt/inverter-tools
RUN git submodule update --init
RUN mkdir build
WORKDIR /opt/inverter-tools/build
RUN cmake ..
RUN make

# Pour inspection ou copie des binaires
CMD ["/bin/bash"]