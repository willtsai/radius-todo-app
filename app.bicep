extension radius

param environment string

param image string = 'ghcr.io/radius-project/samples/demo:latest'

resource radiustodoapp 'Applications.Core/applications@2023-10-01-preview' = {
  name: 'radius-todo-app'
  properties: {
    environment: environment
  }
}

resource demo 'Applications.Core/containers@2023-10-01-preview' = {
  name: 'demo'
  properties: {
    application: radiustodoapp.id
    container: {
      image: image
      ports: {
        web: {
          containerPort: 3000
        }
      }
      livenessProbe: {
        kind: 'httpGet'
        containerPort: 3000
        path: '/healthz'
        initialDelaySeconds: 10
      }
    }
    connections: {
      redis: {
        source: db.id
      }
    }
  }
}

resource db 'Applications.Datastores/redisCaches@2023-10-01-preview' = {
  name: 'db'
  properties: {
    application: radiustodoapp.id
    environment: environment
  }
}
