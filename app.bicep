extension radius

param environment string = 'default'

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
      sql: {
        source: sqlDb.id
      }
    }
  }
}

resource sqlDb 'Applications.Datastores/sqlDatabases@2023-10-01-preview' = {
  name: 'sqlDb'
  properties: {
    environment: environment
    application: radiustodoapp.id
  }
}

resource frontend 'Applications.Core/containers@2023-10-01-preview' = {
  name: 'frontend'
  properties: {
    application: radiustodoapp.id
    container: {
      image: image
      ports: {
        web: {
          containerPort: 3001
        }
      }
    }
    connections: {
      demo: {
        source: demo.id
      }
    }
  }
}

resource gateway 'Applications.Core/gateways@2023-10-01-preview' = {
  name: 'gateway'
  properties: {
    application: radiustodoapp.id
    routes: [
      {
        path: '/'
        destination: 'http://${frontend.name}:3001'
      }
    ]
  }
}
