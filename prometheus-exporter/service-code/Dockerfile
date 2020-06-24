FROM centos:8

ADD pip-requirements.txt /tmp/requirements.txt
RUN adduser prom --uid 1000
RUN dnf -y install vim python3 python3-devel net-tools lsof && \
    dnf -y groupinstall "Development Tools" && \
    mkdir -p /opt/redfish-exporter
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt
ADD *.py server.ini /opt/redfish-exporter/
USER prom
EXPOSE 8000
ENTRYPOINT [ "uwsgi", "/opt/redfish-exporter/server.ini" ]
