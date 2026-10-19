FROM pgvector/pgvector:pg16
COPY entrypoint.sh /usr/local/bin/render-entrypoint.sh
RUN chmod +x /usr/local/bin/render-entrypoint.sh
EXPOSE 5432
ENTRYPOINT ["/usr/local/bin/render-entrypoint.sh"]
