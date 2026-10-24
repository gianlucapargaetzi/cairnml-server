#!/usr/bin/env bash
set -e

mkdir -p /var/log/cairnml

SERVER_TYPE=$1

if (( $# < 1 )) ; then
    echo "The server type was not stated. It should be either apiserver, webserver or async_delete."
    sleep 60
    exit 1

elif [[ ${SERVER_TYPE} == "apiserver" ]]; then
    cd /opt/cairnml/

    if [[ -n $CAIRNML_USE_GUNICORN ]]; then
      MAX_REQUESTS=
      if [[ -n $CAIRNML_GUNICORN_MAX_REQUESTS ]]; then
        MAX_REQUESTS="--max-requests $CAIRNML_GUNICORN_MAX_REQUESTS"
        if [[ -n $CAIRNML_GUNICORN_MAX_REQUESTS_JITTER ]]; then
          MAX_REQUESTS="$MAX_REQUESTS --max-requests-jitter $CAIRNML_GUNICORN_MAX_REQUESTS_JITTER"
        fi
      fi

      export GUNICORN_CMD_ARGS=${CAIRNML_GUNICORN_CMD_ARGS}

      # Note: don't be tempted to "fix" $MAX_REQUESTS with "$MAX_REQUESTS" as this produces an empty arg which fucks up gunicorn
      gunicorn \
        -w "${CAIRNML_GUNICORN_WORKERS:-8}" \
        -t "${CAIRNML_GUNICORN_TIMEOUT:-600}" --bind="${CAIRNML_GUNICORN_BIND:-0.0.0.0:8008}" \
        $MAX_REQUESTS apiserver.server:app
    else
        python3 -m apiserver.server
    fi

elif [[ ${SERVER_TYPE} == "webserver" ]]; then

    if [[ "${USER_KEY}" != "" ]] || [[ "${USER_SECRET}" != "" ]] || [[ "${COMPANY_ID}" != "" ]]; then
      cat << EOF > /usr/share/nginx/html/credentials.json
{
  "userKey": "${USER_KEY}",
  "userSecret": "${USER_SECRET}",
  "companyID": "${COMPANY_ID}"
}
EOF
    fi

    # Create an empty configuration json
    echo "{}" > /tmp/configuration.json
	
    # Copy the external configuration file if it exists
    if test -f "/mnt/external_files/configs/configuration.json"; then
      echo "Copying external configuration"
      cp /mnt/external_files/configs/configuration.json /tmp/configuration.json
    fi

	  # Update from env variables
    echo "Updating configuration from env"
    /opt/cairnml/utilities/update_from_env.py \
        --verbose \
        /tmp/configuration.json \
        /usr/share/nginx/html/configuration.json

    export NGINX_APISERVER_ADDR=${NGINX_APISERVER_ADDRESS:-http://apiserver:8008}
    export COMMENT_IPV6_LISTEN=$([ "$DISABLE_NGINX_IPV6" = "true" ] && echo "#" || echo "")
    envsubst '${COMMENT_IPV6_LISTEN} ${NGINX_APISERVER_ADDR}' < /etc/nginx/cairnml.conf.template > /etc/nginx/sites-enabled/default

    if [[ -n "${CAIRNML_SERVER_SUB_PATH}" ]]; then
      mkdir -p /etc/nginx/default.d/
      envsubst '${CAIRNML_SERVER_SUB_PATH}' < /etc/nginx/cairnml_subpath.conf.template > /etc/nginx/default.d/cairnml_subpath.conf
      cp /usr/share/nginx/html/env.js /usr/share/nginx/html/env.js.origin
      envsubst '${CAIRNML_SERVER_SUB_PATH}' < /usr/share/nginx/html/env.js.origin > /usr/share/nginx/html/env.js
      cp /usr/share/nginx/html/index.html /usr/share/nginx/html/index.html.origin
      sed 's/href="\/"/href="\/'${CAIRNML_SERVER_SUB_PATH}'\/"/' /usr/share/nginx/html/index.html.origin > /usr/share/nginx/html/index.html
    fi

    #start the server
    /usr/sbin/nginx -g "daemon off;"

elif [[ ${SERVER_TYPE} == "async_delete" ]]; then
    # Removes the objects of deleted tasks and models from storage (the URLs are queued by the API server).
    cd /opt/cairnml/
    python3 -m apiserver.jobs.async_urls_delete

else
    echo "Server type ${SERVER_TYPE} is invalid. Please choose either apiserver, webserver or async_delete."
fi
