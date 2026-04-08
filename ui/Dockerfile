FROM node:lts-alpine

WORKDIR /app

# copy both 'package.json' and 'package-lock.json' (if available)
COPY package*.json ./

# install project dependencies
RUN npm install

# copy project files and folders to the current working directory (i.e. 'app' folder)
COPY . .

ENV PORT 5173
EXPOSE 5173

CMD [ "npm", "run", "dev", "--", "--host"]